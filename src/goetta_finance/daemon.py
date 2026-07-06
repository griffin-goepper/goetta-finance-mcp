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
from pathlib import Path
from typing import Any

import duckdb
import uvicorn
from fastapi import FastAPI
from mcp.server.fastmcp import FastMCP

from goetta_finance.collector import collect_under_lock
from goetta_finance.errors import GoettaFinanceError
from goetta_finance.goals import goal_breach_warnings
from goetta_finance.server import build_server
from goetta_finance.simplefin import SimpleFinClient
from goetta_finance.store import FinanceStore

# Daemon lifecycle is coupled to the concrete backend on purpose: DuckDB's
# FatalException invalidates the whole in-process database, and "restart the
# process" is the only recovery. The FinanceStore protocol stays
# backend-agnostic.
from goetta_finance.store.duckdb_store import is_database_invalidated
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


def _run_collect_blocking(
    store: FinanceStore,
    client: SimpleFinClient,
    on_fatal: Callable[[], None] | None = None,
) -> None:
    """Worker-thread entry. Eats exceptions so the scheduler keeps running —
    except a fatal store error, which triggers ``on_fatal``: once DuckDB
    invalidates the in-process database, every later query on every surface
    fails until the process reopens the file, so staying alive just serves
    500s (the 2026-07-06 zombie-daemon incident). Exiting lets the
    supervisor restart us into a healthy state."""
    try:
        result = collect_under_lock(store, client)
        if result is None:
            logger.info("scheduled sync skipped — another sync in flight")
            return
        # Post-sync goal breach summary. WARNING because a breached
        # threshold is the one thing the user asked to be told about;
        # messages carry goal/category/account names and amounts only,
        # never transaction text. Evaluation failures must not make a
        # successful sync look failed.
        try:
            for line in goal_breach_warnings(store):
                logger.warning("goal breach: %s", line)
        except GoettaFinanceError:
            logger.exception("goal evaluation after scheduled sync failed")
    except Exception as exc:
        logger.exception("scheduled sync raised")
        if on_fatal is not None and is_database_invalidated(exc):
            logger.critical(
                "database invalidated by a fatal DuckDB error — shutting down "
                "so the supervisor can restart with a fresh process"
            )
            on_fatal()


async def _scheduler_loop(
    store: FinanceStore,
    client: SimpleFinClient,
    sync_at: str,
    on_fatal: Callable[[], None] | None = None,
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
            await asyncio.to_thread(_run_collect_blocking, store, client, on_fatal)
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
        await asyncio.to_thread(_run_collect_blocking, store, client, on_fatal)


async def _stop_file_watch_loop(
    stop_file: Path,
    request_shutdown: Callable[[], None],
    poll_seconds: float = 2.0,
) -> None:
    """Poll for ``stop_file``; request a graceful shutdown when it appears.

    Why a file and not an HTTP endpoint: a ``POST /shutdown`` on an
    unauthenticated localhost server is reachable by any local process
    AND by any web page the user visits (``fetch(..., {mode: 'no-cors'})``
    is not blocked by CORS for fire-and-forget requests) — a drive-by
    denial of service. A file in the data directory requires local
    filesystem access, which already implies full control of the DB.

    Why graceful shutdown matters: killing the daemon hard can freeze
    uncheckpointed WAL content; if that WAL holds DDL, DuckDB's replay
    fails and the database won't open (the 2026-07-05 incident). A
    graceful exit closes the store, which checkpoints.

    A stop file that already exists at startup triggers an immediate
    shutdown — same contract as the supervisor pattern (a launcher that
    respects the stop file refuses to restart while it exists; the
    daemon refusing to run mirrors it). The file is never deleted by the
    daemon: removing it is the operator's re-arm step.
    """
    if stop_file.exists():
        logger.warning(
            "stop file %s already present at startup — shutting down; delete it to run the daemon",
            stop_file,
        )
        request_shutdown()
        return
    while True:
        await asyncio.sleep(poll_seconds)
        if stop_file.exists():
            logger.info("stop file %s present — shutting down gracefully", stop_file)
            request_shutdown()
            return


def _build_lifespan(
    store: FinanceStore,
    client: SimpleFinClient,
    sync_at: str,
    schedule_enabled: bool,
    mcp_server: FastMCP | None,
    stop_file: Path | None = None,
    request_shutdown: Callable[[], None] | None = None,
    stop_poll_seconds: float = 2.0,
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
            tasks: list[asyncio.Task[None]] = []
            if schedule_enabled:
                tasks.append(
                    asyncio.create_task(
                        # request_shutdown doubles as the fatal-error escape
                        # hatch: an invalidated database is unrecoverable
                        # in-process, so the sync loop asks for the same
                        # graceful exit the stop file would.
                        _scheduler_loop(store, client, sync_at, on_fatal=request_shutdown),
                        name="goetta-finance-scheduler",
                    )
                )
            if stop_file is not None and request_shutdown is not None:
                tasks.append(
                    asyncio.create_task(
                        _stop_file_watch_loop(
                            stop_file, request_shutdown, poll_seconds=stop_poll_seconds
                        ),
                        name="goetta-finance-stop-watch",
                    )
                )
            try:
                yield
            finally:
                for task in tasks:
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
    stop_file: Path | None = None,
    request_shutdown: Callable[[], None] | None = None,
    stop_poll_seconds: float = 2.0,
    dash_dir: Path | None = None,
) -> FastAPI:
    """Construct the daemon's FastAPI app without binding a port.

    Split out from ``run_daemon`` so tests can drive uvicorn manually
    (for graceful start/stop) without re-implementing the wiring.
    ``stop_file`` + ``request_shutdown`` arm the graceful-shutdown watch:
    when the file appears, ``request_shutdown()`` is called (in
    ``run_daemon`` it flips uvicorn's ``should_exit``).
    """
    parse_sync_at(sync_at)  # validate early
    mcp = build_server(store, client=client) if mcp_enabled else None
    # Order matters: build_app mounts mcp.streamable_http_app() (which
    # lazily creates session_manager). _build_lifespan then enters
    # session_manager.run() — must happen *after* the mount so the
    # session manager exists.
    lifespan = _build_lifespan(
        store,
        client,
        sync_at,
        schedule_enabled,
        mcp,
        stop_file=stop_file,
        request_shutdown=request_shutdown,
        stop_poll_seconds=stop_poll_seconds,
    )
    app = build_app(store, mcp_server=mcp, lifespan=lifespan, dash_dir=dash_dir)
    if request_shutdown is not None:
        _register_fatal_error_handler(app, request_shutdown)
    return app


def _register_fatal_error_handler(app: FastAPI, request_shutdown: Callable[[], None]) -> None:
    """Turn a FatalException surfacing through any HTTP request (dashboard,
    ``/api/v1``, MCP mount) into a graceful daemon exit.

    Once DuckDB invalidates the in-process database, every request fails
    until the process reopens the file; without this the daemon zombies —
    up, answering, every response a 500 (observed live 2026-07-06, all
    morning). The supervisor only heals process exits, so exit.

    Best-effort by design: exceptions inside FastMCP tool handlers are
    converted to MCP error results internally and never propagate here —
    but any dashboard/JSON-API request after the invalidation does, as does
    the next scheduled sync (which has its own ``on_fatal`` hook).
    """

    def _handle(request: Any, exc: Exception) -> Any:
        from fastapi.responses import JSONResponse

        logger.critical(
            "database invalidated by a fatal DuckDB error (via %s) — shutting down "
            "so the supervisor can restart with a fresh process",
            request.url.path,
        )
        request_shutdown()
        return JSONResponse(
            status_code=500,
            content={"error": "database invalidated; daemon is restarting"},
        )

    app.add_exception_handler(duckdb.FatalException, _handle)


def run_daemon(
    store: FinanceStore,
    client: SimpleFinClient,
    *,
    host: str = "127.0.0.1",
    port: int = 8765,
    sync_at: str = "06:00",
    schedule_enabled: bool = True,
    mcp_enabled: bool = True,
    stop_file: Path | None = None,
    dash_dir: Path | None = None,
) -> None:
    """Run the goetta-finance daemon: dashboard + MCP HTTP + scheduler.

    Blocks until interrupted (Ctrl-C) or, when ``stop_file`` is given,
    until that file appears — the graceful alternative to killing the
    process (a hard kill can freeze DDL in the WAL and brick the DB;
    see the 0009 incident note in duckdb_store.init). Releases the
    scheduler task and closes the store cleanly on shutdown.
    """
    # Late-bound so the lifespan (built before the server object exists)
    # can flip uvicorn's should_exit. uvicorn polls it in its main loop
    # and runs its full graceful-shutdown sequence.
    server_ref: list[uvicorn.Server] = []

    def request_shutdown() -> None:
        if server_ref:
            server_ref[0].should_exit = True

    app = build_daemon_app(
        store,
        client,
        sync_at=sync_at,
        schedule_enabled=schedule_enabled,
        mcp_enabled=mcp_enabled,
        stop_file=stop_file,
        request_shutdown=request_shutdown,
        dash_dir=dash_dir,
    )
    logger.info(
        "goetta-finance daemon: http://%s:%d  mcp=%s  schedule=%s @ %s  stop_file=%s",
        host,
        port,
        mcp_enabled,
        schedule_enabled,
        sync_at,
        stop_file,
    )
    server = uvicorn.Server(uvicorn.Config(app, host=host, port=port, log_level="warning"))
    server_ref.append(server)
    server.run()
