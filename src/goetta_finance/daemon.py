"""goetta-finance daemon: one long-lived process hosting MCP, the
dashboard, and a scheduled-sync loop.

Why a daemon at all: on Windows, DuckDB takes an exclusive OS file lock
on ``data.duckdb`` even for a read-only handle, so running ``serve`` and
``web`` as separate processes simultaneously fails. The daemon owns one
read-write store, exposes MCP over streamable-HTTP at ``/api/mcp``, serves
the dashboard at ``/``, and runs the scheduler in the same event loop.
"""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import AsyncIterator, Callable
from contextlib import (
    AbstractAsyncContextManager,
    AsyncExitStack,
    asynccontextmanager,
    suppress,
)
from datetime import datetime, timedelta
from typing import Any

import uvicorn
from fastapi import FastAPI
from mcp.server.fastmcp import FastMCP

from goetta_finance.collector import collect_under_lock
from goetta_finance.server import build_server
from goetta_finance.simplefin import SimpleFinClient
from goetta_finance.store import FinanceStore
from goetta_finance.web.app import build_app

logger = logging.getLogger(__name__)

_SYNC_AT_RE = re.compile(r"^([01]?\d|2[0-3]):([0-5]?\d)$")


def parse_sync_at(value: str) -> tuple[int, int]:
    """Parse an ``HH:MM`` 24-hour time. Raises ``ValueError`` on bad input."""
    match = _SYNC_AT_RE.match(value.strip())
    if not match:
        raise ValueError(f"sync_at must be HH:MM (24-hour); got {value!r}")
    return int(match.group(1)), int(match.group(2))


def _next_tick(now: datetime, hour: int, minute: int) -> datetime:
    target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    return target


def _most_recent_past_tick(now: datetime, hour: int, minute: int) -> datetime:
    target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if target > now:
        target -= timedelta(days=1)
    return target


def _run_collect_blocking(store: FinanceStore, client: SimpleFinClient) -> None:
    """Worker-thread entry. Eats exceptions so the scheduler keeps running."""
    try:
        result = collect_under_lock(store, client)
        if result is None:
            logger.info("scheduled sync skipped — another sync in flight")
    except Exception:
        logger.exception("scheduled sync raised")


async def _scheduler_loop(
    store: FinanceStore, client: SimpleFinClient, sync_at: str
) -> None:
    """Run a sync at ``sync_at`` local time every day.

    On entry, checks whether the most recent scheduled tick was missed
    (laptop closed past 6am, daemon just woke up, last sync was before
    that tick) and runs immediately if so. Then sleeps to the next tick.
    """
    hour, minute = parse_sync_at(sync_at)
    while True:
        now = datetime.now().astimezone()
        last = store.last_sync_time()
        last_tick = _most_recent_past_tick(now, hour, minute)
        # If last_sync is None or older than the most recent scheduled tick,
        # we slept through it — catch up before sleeping again.
        if last is None or last < last_tick:
            logger.info(
                "scheduler: catching up missed sync (tick was %s, last sync %s)",
                last_tick.isoformat(timespec="minutes"),
                last.isoformat(timespec="minutes") if last else "never",
            )
            await asyncio.to_thread(_run_collect_blocking, store, client)
            now = datetime.now().astimezone()
        next_tick = _next_tick(now, hour, minute)
        sleep_seconds = max(0.0, (next_tick - now).total_seconds())
        logger.info(
            "scheduler: next sync at %s (sleeping %.0fs)",
            next_tick.isoformat(timespec="minutes"),
            sleep_seconds,
        )
        await asyncio.sleep(sleep_seconds)
        logger.info("scheduler: running scheduled sync")
        await asyncio.to_thread(_run_collect_blocking, store, client)


def _build_lifespan(
    store: FinanceStore,
    client: SimpleFinClient,
    sync_at: str,
    schedule_enabled: bool,
    mcp_server: FastMCP | None,
) -> Callable[[FastAPI], AbstractAsyncContextManager[Any]]:
    """Build a FastAPI lifespan that owns the scheduler task AND the MCP
    session manager.

    Two things must happen inside the lifespan:

    1. The scheduler asyncio task is started (cancelled cleanly on exit).
    2. ``mcp_server.session_manager.run()`` is entered. ``FastMCP``'s
       ``streamable_http_app()`` ships its own Starlette lifespan that
       does this, but FastAPI does NOT invoke mounted sub-apps' lifespans
       — so when we mount the MCP Starlette app under ``/api``, its
       lifespan never fires and the session manager's task group stays
       uninitialized. The first MCP request then explodes with
       ``RuntimeError: Task group is not initialized.`` Tracked by
       ``tests/test_daemon.py::test_daemon_mcp_endpoint_handles_real_call``.

    Using ``asynccontextmanager`` rather than the deprecated
    ``@app.on_event("startup")``. ``AsyncExitStack`` so both contexts
    unwind in reverse-init order on Ctrl-C.
    """

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        async with AsyncExitStack() as stack:
            if mcp_server is not None:
                await stack.enter_async_context(mcp_server.session_manager.run())
            task: asyncio.Task[None] | None = None
            if schedule_enabled:
                task = asyncio.create_task(
                    _scheduler_loop(store, client, sync_at),
                    name="goetta-finance-scheduler",
                )
            try:
                yield
            finally:
                if task is not None:
                    task.cancel()
                    with suppress(asyncio.CancelledError):
                        await task

    return lifespan


def build_daemon_app(
    store: FinanceStore,
    client: SimpleFinClient,
    *,
    sync_at: str = "06:00",
    schedule_enabled: bool = True,
    mcp_enabled: bool = True,
) -> FastAPI:
    """Construct the daemon's FastAPI app without binding a port.

    Split out from ``run_daemon`` so tests can drive uvicorn manually
    (for graceful start/stop) without re-implementing the wiring.
    """
    parse_sync_at(sync_at)  # validate early
    mcp = build_server(store, client=client) if mcp_enabled else None
    # Order matters: build_app mounts mcp.streamable_http_app() (which
    # lazily creates session_manager). _build_lifespan then enters
    # session_manager.run() — must happen *after* the mount so the
    # session manager exists.
    lifespan = _build_lifespan(store, client, sync_at, schedule_enabled, mcp)
    return build_app(store, mcp_server=mcp, lifespan=lifespan)


def run_daemon(
    store: FinanceStore,
    client: SimpleFinClient,
    *,
    host: str = "127.0.0.1",
    port: int = 8765,
    sync_at: str = "06:00",
    schedule_enabled: bool = True,
    mcp_enabled: bool = True,
) -> None:
    """Run the goetta-finance daemon: dashboard + MCP HTTP + scheduler.

    Blocks until interrupted (Ctrl-C). Releases the scheduler task and
    closes the store cleanly on shutdown.
    """
    app = build_daemon_app(
        store,
        client,
        sync_at=sync_at,
        schedule_enabled=schedule_enabled,
        mcp_enabled=mcp_enabled,
    )
    logger.info(
        "goetta-finance daemon: http://%s:%d  mcp=%s  schedule=%s @ %s",
        host,
        port,
        mcp_enabled,
        schedule_enabled,
        sync_at,
    )
    uvicorn.run(app, host=host, port=port, log_level="warning")
