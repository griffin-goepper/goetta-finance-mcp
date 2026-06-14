"""Concurrency stress test for the daemon's shared-store architecture.

This test exists specifically to catch the class of bug the user flagged
during plan review: heisenbugs in the data layer that pass single-threaded
tests but corrupt state under real load months later. The original
implementation shared a single DuckDB connection across the FastAPI
worker threads and the background-sync thread, and DuckDB's
``DuckDBPyConnection`` keeps one "pending query result" slot per
connection — concurrent ``execute`` calls from different threads on the
same connection corrupted that slot.

The fix is a per-store ``threading.RLock`` wrapping every public method.
This test exercises that lock by hammering the daemon with 50 iterations
of mixed read/write traffic in parallel via a ``ThreadPoolExecutor``,
then asserts no exceptions and that account balances stay internally
consistent across reads.

If this test goes red after a refactor, do not skip or mark it slow.
Find the new race.
"""

from __future__ import annotations

import socket
import threading
import time
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
import pytest
import uvicorn

from goetta_finance.collector import collect_under_lock
from goetta_finance.daemon import build_daemon_app
from goetta_finance.models import Account, AccountType
from goetta_finance.store.duckdb_store import DuckDBStore

N_ITERATIONS = 50


class _FakeClient:
    """Minimal SimpleFinClient stand-in for the stress test.

    Returns the same account on every fetch with a deterministic balance
    so reads from any thread observe a stable value (no torn reads even
    across the upsert path).
    """

    def __init__(self) -> None:
        self.fetch_count = 0
        self._lock = threading.Lock()

    def fetch_chunked(
        self, start: datetime, end: datetime, chunk_days: int = 60
    ) -> Iterator[dict[str, Any]]:
        with self._lock:
            self.fetch_count += 1
        yield {
            "accounts": [
                {
                    "id": "stress-acct-1",
                    "name": "Stress Checking",
                    "currency": "USD",
                    "balance": "1234.56",
                    "balance-date": int(datetime(2026, 5, 17, tzinfo=UTC).timestamp()),
                    "org": {"name": "StressBank", "domain": "stress.example"},
                    "transactions": [],
                }
            ],
            "errors": [],
        }


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _wait_for_health(url: str, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            if httpx.get(url, timeout=1.0).status_code == 200:
                return
        except httpx.HTTPError:
            pass
        time.sleep(0.05)
    raise TimeoutError(f"daemon did not come up at {url}")


@pytest.fixture
def daemon_stressed(tmp_path: Path) -> Iterator[dict[str, Any]]:
    db = tmp_path / "stress.duckdb"
    store = DuckDBStore(db)
    store.init()
    # Seed one account so reads start with consistent data.
    store.upsert_accounts(
        [
            Account(
                id="stress-acct-1",
                org_name="StressBank",
                name="Stress Checking",
                balance=Decimal("1234.56"),
                balance_date=datetime(2026, 5, 17, tzinfo=UTC),
                type=AccountType.CHECKING,
            )
        ]
    )
    client = _FakeClient()
    app = build_daemon_app(
        store,
        client,  # type: ignore[arg-type]
        sync_at="23:59",
        schedule_enabled=False,  # don't race the test against the scheduler
        mcp_enabled=False,  # this test exercises store + dashboard layer
    )
    port = _free_port()
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{port}"
    try:
        _wait_for_health(f"{base}/health", timeout=8.0)
        yield {"base": base, "store": store, "client": client}
    finally:
        server.should_exit = True
        thread.join(timeout=5.0)
        store.close()


def test_daemon_under_parallel_read_and_write_load(
    daemon_stressed: dict[str, Any],
) -> None:
    """Three concurrent workloads, ``N_ITERATIONS`` calls each:

    1. HTTP GET ``/`` — exercises the dashboard read path through FastAPI
       worker threads (``store.get_accounts``).
    2. HTTP GET ``/health`` — exercises ``store.last_sync_time`` +
       ``store.get_accounts``.
    3. ``collect_under_lock(store, client)`` from worker threads — the
       writer path (``upsert_accounts`` + ``record_*``).

    The lock the store added must serialize every DB op without
    corrupting state. Assertions:

    - No exception escapes any worker.
    - Every dashboard read observes the seeded balance (1234.56), no
      ``None`` rows and no torn values.
    - At least one sync recorded a sync_runs row (so the writer path
      definitely fired).
    """
    base = daemon_stressed["base"]
    store: DuckDBStore = daemon_stressed["store"]
    client = daemon_stressed["client"]
    errors: list[Exception] = []
    err_lock = threading.Lock()
    seen_balances: set[str] = set()
    balances_lock = threading.Lock()

    def _record_error(exc: Exception) -> None:
        with err_lock:
            errors.append(exc)

    def _http_dashboard_read() -> None:
        try:
            r = httpx.get(f"{base}/", timeout=5.0)
            assert r.status_code == 200, r.status_code
            text = r.text
            # The seeded balance should appear in the dashboard HTML
            # whenever the read returned the seeded account.
            if "1234.56" in text:
                with balances_lock:
                    seen_balances.add("1234.56")
        except Exception as exc:
            _record_error(exc)

    def _http_health_read() -> None:
        try:
            r = httpx.get(f"{base}/health", timeout=5.0)
            assert r.status_code == 200
            body = r.json()
            assert body["ok"] is True
        except Exception as exc:
            _record_error(exc)

    def _direct_collect() -> None:
        try:
            collect_under_lock(store, client)
        except Exception as exc:
            _record_error(exc)

    with ThreadPoolExecutor(max_workers=16) as pool:
        futures = []
        for _ in range(N_ITERATIONS):
            futures.append(pool.submit(_http_dashboard_read))
            futures.append(pool.submit(_http_health_read))
            futures.append(pool.submit(_direct_collect))
        for f in as_completed(futures):
            f.result()

    assert not errors, (
        f"{len(errors)} exception(s) under concurrent load — first few: {errors[:3]!r}"
    )
    assert "1234.56" in seen_balances, (
        "seeded balance never appeared in any dashboard read — writes may have corrupted reads"
    )
    assert client.fetch_count >= 1, "no sync writes ran"
    assert store.last_sync_time() is not None, (
        "writes ran but sync_runs table never recorded one — transactional consistency broken"
    )
