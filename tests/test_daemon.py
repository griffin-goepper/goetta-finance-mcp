"""End-to-end tests for the goetta-finance daemon.

Spins up uvicorn in a background thread, hits the dashboard, the
``/health`` probe, and the streamable-HTTP MCP endpoint over real HTTP,
then shuts down cleanly. Verifies the lifespan cancels the scheduler
task on exit (no orphan threads).
"""

from __future__ import annotations

import socket
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
import uvicorn

from goetta_finance.daemon import build_daemon_app
from goetta_finance.store.duckdb_store import DuckDBStore


class _FakeClient:
    def __init__(self) -> None:
        self.fetch_count = 0

    def fetch_chunked(self, start: Any, end: Any, chunk_days: int = 60) -> Iterator[dict[str, Any]]:
        self.fetch_count += 1
        yield {"accounts": [], "errors": []}


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = int(s.getsockname()[1])
    return port


def _wait_for_health(url: str, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    last_err: Exception | None = None
    while time.monotonic() < deadline:
        try:
            response = httpx.get(url, timeout=1.0)
            if response.status_code == 200:
                return
        except httpx.HTTPError as exc:
            last_err = exc
        time.sleep(0.05)
    raise TimeoutError(f"daemon did not come up at {url}: last error {last_err!r}")


def _scheduler_task_alive() -> bool:
    """True if the daemon's scheduler asyncio task is still active.

    The scheduler runs as an asyncio task, not an OS thread, so we can't
    enumerate it via ``threading.enumerate``. Best proxy: check if the
    uvicorn worker thread is alive (named "goetta-finance-scheduler"
    won't appear because asyncio tasks don't get OS-thread names).
    Instead, this helper just confirms no orphan OS threads were left
    behind by the daemon — the asyncio task cancellation is verified by
    the daemon shutting down within the timeout below.
    """
    return any(t.name == "goetta-finance-scheduler" and t.is_alive() for t in threading.enumerate())


@pytest.fixture
def daemon_at_random_port(tmp_path: Path) -> Iterator[dict[str, Any]]:
    """Start the daemon on a free port; tear down cleanly after the test.

    Scheduler is disabled here so the test isn't racing the catch-up sync
    that fires on startup against a fresh store. The catch-up + lifespan
    behaviour gets its own dedicated test below.
    """
    db = tmp_path / "daemon.duckdb"
    store = DuckDBStore(db)
    store.init()
    client = _FakeClient()
    app = build_daemon_app(
        store,
        client,  # type: ignore[arg-type]
        sync_at="23:59",
        schedule_enabled=False,
        mcp_enabled=True,
    )
    port = _free_port()
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, name="daemon-uvicorn", daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{port}"
    try:
        _wait_for_health(f"{base}/health", timeout=8.0)
        yield {"base": base, "port": port, "store": store, "client": client}
    finally:
        server.should_exit = True
        thread.join(timeout=5.0)
        store.close()
        assert not thread.is_alive(), "uvicorn thread did not exit on shutdown"


def test_daemon_health_endpoint_returns_ok(daemon_at_random_port: dict[str, Any]) -> None:
    base = daemon_at_random_port["base"]
    response = httpx.get(f"{base}/health", timeout=3.0)
    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert body["mcp_enabled"] is True
    assert body["last_sync"] is None
    assert body["accounts"] == 0


def test_daemon_serves_dashboard_root(daemon_at_random_port: dict[str, Any]) -> None:
    base = daemon_at_random_port["base"]
    response = httpx.get(f"{base}/", timeout=3.0)
    assert response.status_code == 200
    # The accounts template should mention the brand name.
    assert "goetta-finance" in response.text or "Accounts" in response.text


def test_daemon_mcp_endpoint_handles_real_initialize(
    daemon_at_random_port: dict[str, Any],
) -> None:
    """Run a real MCP ``initialize`` handshake over HTTP.

    A loose 'not 404' assertion is not enough: when FastMCP's session
    manager isn't run as a lifespan (the bug that bit us during hand-test
    — FastAPI doesn't invoke mounted sub-apps' lifespans, so
    ``streamable_http_app()``'s built-in lifespan never fires), the
    endpoint comes up but every request 500s with ``RuntimeError: Task
    group is not initialized``. ``status_code != 404`` happily allows
    that.

    A successful initialize round-trip proves the session manager is
    running. Failure mode: a 5xx response or a JSON-RPC ``error`` block.
    """
    base = daemon_at_random_port["base"]
    response = httpx.post(
        f"{base}/api/mcp",
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-03-26",
                "capabilities": {},
                "clientInfo": {"name": "goetta-daemon-test", "version": "0"},
            },
        },
        headers={"accept": "application/json, text/event-stream"},
        timeout=5.0,
    )
    assert response.status_code < 500, (
        f"MCP endpoint server-errored on initialize "
        f"(status {response.status_code}): {response.text[:400]!r}"
    )
    assert response.status_code == 200, (
        f"MCP initialize did not succeed (status {response.status_code}): {response.text[:400]!r}"
    )
    body_text = response.text
    # streamable-http may return JSON or text/event-stream; both should
    # contain an ``initialize`` result, not a JSON-RPC error envelope.
    assert '"error"' not in body_text or '"result"' in body_text, (
        f"MCP initialize returned an error envelope: {body_text[:400]!r}"
    )
    assert "protocolVersion" in body_text or "serverInfo" in body_text, (
        f"MCP initialize response missing handshake fields: {body_text[:400]!r}"
    )


def test_daemon_no_mcp_disables_endpoint(tmp_path: Path) -> None:
    """With --no-mcp the /api/mcp route should not be mounted."""
    db = tmp_path / "daemon2.duckdb"
    store = DuckDBStore(db)
    store.init()
    client = _FakeClient()
    app = build_daemon_app(
        store,
        client,  # type: ignore[arg-type]
        sync_at="23:59",
        schedule_enabled=False,
        mcp_enabled=False,
    )
    port = _free_port()
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    try:
        _wait_for_health(f"http://127.0.0.1:{port}/health", timeout=8.0)
        response = httpx.post(
            f"http://127.0.0.1:{port}/api/mcp",
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
            timeout=3.0,
        )
        assert response.status_code == 404
        health = httpx.get(f"http://127.0.0.1:{port}/health", timeout=3.0).json()
        assert health["mcp_enabled"] is False
    finally:
        server.should_exit = True
        thread.join(timeout=5.0)
        store.close()


def test_daemon_scheduler_catches_up_missed_tick(tmp_path: Path) -> None:
    """If last_sync is older than the most recent scheduled tick, the
    scheduler runs a sync immediately on startup. Verifies the
    sleep-through-the-6am-tick recovery path."""
    db = tmp_path / "daemon_catchup.duckdb"
    store = DuckDBStore(db)
    store.init()
    client = _FakeClient()
    app = build_daemon_app(
        store,
        client,  # type: ignore[arg-type]
        sync_at="23:59",  # any past-or-future time works; we just need a tick
        schedule_enabled=True,
        mcp_enabled=False,
    )
    port = _free_port()
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    try:
        _wait_for_health(f"http://127.0.0.1:{port}/health", timeout=8.0)
        # Give the scheduler a moment to run the catch-up sync.
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            if client.fetch_count >= 1 and store.last_sync_time() is not None:
                break
            time.sleep(0.05)
        assert client.fetch_count >= 1, "catch-up sync never fired"
        assert store.last_sync_time() is not None, "catch-up sync never recorded a run"
    finally:
        server.should_exit = True
        thread.join(timeout=5.0)
        store.close()


def test_daemon_shutdown_drains_scheduler_within_timeout(tmp_path: Path) -> None:
    """The lifespan cancels and awaits the scheduler task on shutdown.
    If that's broken, uvicorn hangs waiting for the sleeping task. This
    test enforces the shutdown completes well under the join timeout."""
    db = tmp_path / "daemon3.duckdb"
    store = DuckDBStore(db)
    store.init()
    client = _FakeClient()
    app = build_daemon_app(
        store,
        client,  # type: ignore[arg-type]
        sync_at="23:59",
        schedule_enabled=True,
        mcp_enabled=False,
    )
    port = _free_port()
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    try:
        _wait_for_health(f"http://127.0.0.1:{port}/health", timeout=8.0)
    finally:
        start = time.monotonic()
        server.should_exit = True
        thread.join(timeout=10.0)
        elapsed = time.monotonic() - start
        store.close()
    assert not thread.is_alive(), "daemon did not shut down — lifespan likely broken"
    assert elapsed < 8.0, f"shutdown took {elapsed:.1f}s — scheduler not cancelled cleanly"


def test_run_collect_blocking_logs_goal_breaches(
    store: DuckDBStore,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """After a scheduled sync, breached goals land in the daemon log at
    WARNING (goal/category names and amounts only — never transaction
    text). Uses the store fixture directly; collect is stubbed."""
    import logging
    from datetime import UTC, datetime
    from decimal import Decimal

    from goetta_finance import daemon as daemon_module
    from goetta_finance.models import Account, AccountType, SyncRun, Transaction

    store.upsert_accounts(
        [
            Account(
                id="d-goal",
                name="Daemon Checking",
                balance=Decimal("100.00"),
                balance_date=datetime.now(tz=UTC),
                type=AccountType.CHECKING,
            )
        ]
    )
    store.upsert_transactions(
        [
            Transaction(
                id="d-tx-1",
                account_id="d-goal",
                posted=datetime.now(tz=UTC),
                amount=Decimal("-450.00"),
                description="daemon goal txn",
            )
        ]
    )
    store.set_transaction_override("d-tx-1", "Dining")
    store.add_goal(
        "Dining cap",
        kind="spending_cap",
        amount=Decimal("400"),
        category_name="Dining",
        period="month",
    )

    def fake_collect(s: object, c: object) -> SyncRun:
        now = datetime.now(tz=UTC)
        return SyncRun(started_at=now, finished_at=now)

    monkeypatch.setattr(daemon_module, "collect_under_lock", fake_collect)
    with caplog.at_level(logging.WARNING, logger="goetta_finance.daemon"):
        daemon_module._run_collect_blocking(store, _FakeClient())  # type: ignore[arg-type]
    breach_records = [r for r in caplog.records if "goal breach" in r.getMessage()]
    assert len(breach_records) == 1
    message = breach_records[0].getMessage()
    assert "Dining cap" in message
    assert "450.00" in message
    assert "daemon goal txn" not in message
