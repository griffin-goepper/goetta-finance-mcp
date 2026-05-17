from __future__ import annotations

import asyncio
import contextlib
import threading
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
from mcp.shared.memory import create_connected_server_and_client_session

from goetta_finance import collector
from goetta_finance.models import Account, AccountType, SyncRun, Transaction
from goetta_finance.server import build_server
from goetta_finance.store.duckdb_store import DuckDBStore


def _seed(store: DuckDBStore) -> None:
    store.upsert_accounts(
        [
            Account(
                id="a1",
                org_name="Chase",
                name="Checking",
                balance=Decimal("100.00"),
                balance_date=datetime(2026, 5, 1, tzinfo=UTC),
                type=AccountType.CHECKING,
            )
        ]
    )
    store.upsert_transactions(
        [
            Transaction(
                id="t1",
                account_id="a1",
                posted=datetime(2026, 5, 1, tzinfo=UTC),
                amount=Decimal("-9.99"),
                description="Spotify",
                payee="Spotify",
            )
        ]
    )


def _decode(result: object) -> object:
    """Extract the structured tool return from a call_tool result.

    FastMCP wraps non-dict return types as ``structuredContent = {"result": <value>}``;
    dict return types appear at the top level.
    """
    sc = getattr(result, "structuredContent", None)
    assert isinstance(sc, dict), f"call_tool result has no structuredContent: {result!r}"
    if set(sc.keys()) == {"result"}:
        return sc["result"]
    return sc


@pytest.mark.anyio
async def test_server_lists_expected_tools(store: DuckDBStore) -> None:
    mcp = build_server(store)
    async with create_connected_server_and_client_session(mcp) as session:
        await session.initialize()
        listed = await session.list_tools()
        names = {t.name for t in listed.tools}
        assert names == {
            "list_accounts",
            "get_transactions",
            "account_balance_history",
            "sql_query",
            "sync_now",
            "sync_status",
        }


@pytest.mark.anyio
async def test_server_list_accounts_returns_seeded_data(
    store: DuckDBStore,
) -> None:
    _seed(store)
    mcp = build_server(store)
    async with create_connected_server_and_client_session(mcp) as session:
        await session.initialize()
        result = await session.call_tool("list_accounts", {})
    payload = _decode(result)
    assert isinstance(payload, list)
    assert payload[0]["id"] == "a1"
    assert payload[0]["balance"] == "100.00"


@pytest.mark.anyio
async def test_server_sql_query_returns_rows(
    store: DuckDBStore,
) -> None:
    _seed(store)
    mcp = build_server(store)
    async with create_connected_server_and_client_session(mcp) as session:
        await session.initialize()
        result = await session.call_tool(
            "sql_query",
            {"sql": "SELECT COUNT(*) AS n FROM transactions"},
        )
    payload = _decode(result)
    assert payload == [{"n": 1}]


@pytest.mark.anyio
async def test_server_sql_query_rejects_writes(
    store: DuckDBStore,
) -> None:
    mcp = build_server(store)
    async with create_connected_server_and_client_session(mcp) as session:
        await session.initialize()
        result = await session.call_tool("sql_query", {"sql": "DELETE FROM accounts"})
    assert result.isError is True


@pytest.mark.anyio
async def test_server_sync_now_without_client_reports_error_payload(
    store: DuckDBStore,
) -> None:
    mcp = build_server(store, client=None)
    async with create_connected_server_and_client_session(mcp) as session:
        await session.initialize()
        result = await session.call_tool("sync_now", {})
    payload = _decode(result)
    assert payload["ok"] is False


class _FakeClient:
    """Minimal SimpleFinClient stand-in.

    Counts how many times ``fetch_chunked`` was entered, and can be held in
    flight via ``hold`` so the test can race a second lazy-sync trigger
    against the in-flight collect and prove the lock blocks it.
    """

    def __init__(self) -> None:
        self.fetch_started = threading.Event()
        self.fetch_finished = threading.Event()
        self.proceed = threading.Event()
        self.proceed.set()
        self.fetch_count = 0

    def hold(self) -> None:
        self.proceed.clear()

    def release(self) -> None:
        self.proceed.set()

    def fetch_chunked(
        self, start: datetime, end: datetime, chunk_days: int = 60
    ) -> Any:
        self.fetch_count += 1
        self.fetch_started.set()
        if not self.proceed.wait(timeout=5):
            raise TimeoutError("test never released the fake fetch")
        yield {"accounts": [], "errors": []}
        self.fetch_finished.set()


@pytest.fixture(autouse=True)
def _drain_background_collect_threads() -> Any:
    """Wait for any in-flight bg collect threads to finish before next test.

    Without this, tests that exit while a lazy-sync thread is still racing
    to release the lock either leak the lock into the next test or trip a
    ``release unlocked lock`` RuntimeError when teardown force-releases it.
    """
    yield
    for thread in threading.enumerate():
        if thread.name == "goetta-finance-bg-collect" and thread.is_alive():
            thread.join(timeout=3)
    # Truly defensive: if a test crashed mid-collect with no thread alive
    # to release the lock, free it so the next test starts clean.
    if collector.collect_lock.locked():
        with contextlib.suppress(RuntimeError):
            collector.collect_lock.release()


def _seed_stale_sync(store: DuckDBStore, *, hours_ago: float) -> None:
    """Insert a sync_run finished ``hours_ago`` hours ago so
    ``last_sync_time()`` returns a stale timestamp."""
    stale = datetime.now(tz=UTC) - timedelta(hours=hours_ago)
    store.record_sync_run(
        SyncRun(
            started_at=stale - timedelta(minutes=1),
            finished_at=stale,
        )
    )


@pytest.mark.anyio
async def test_sync_status_reports_freshness_when_no_sync_yet(
    store: DuckDBStore,
) -> None:
    mcp = build_server(store)
    async with create_connected_server_and_client_session(mcp) as session:
        await session.initialize()
        result = await session.call_tool("sync_status", {})
    payload = _decode(result)
    assert isinstance(payload, dict)
    assert payload["last_sync_iso"] is None
    assert payload["data_age_hours"] is None
    assert payload["stale"] is True
    assert payload["sync_in_progress"] is False
    assert payload["staleness_threshold_hours"] > 0


@pytest.mark.anyio
async def test_sync_status_reports_fresh_data(store: DuckDBStore) -> None:
    _seed_stale_sync(store, hours_ago=0.1)  # 6 minutes ago — well under 6h
    mcp = build_server(store)
    async with create_connected_server_and_client_session(mcp) as session:
        await session.initialize()
        result = await session.call_tool("sync_status", {})
    payload = _decode(result)
    assert payload["stale"] is False
    assert payload["data_age_hours"] is not None
    assert payload["data_age_hours"] < 1.0


@pytest.mark.anyio
async def test_lazy_sync_skips_when_data_is_fresh(store: DuckDBStore) -> None:
    _seed_stale_sync(store, hours_ago=0.5)  # well under default 6h threshold
    client = _FakeClient()
    mcp = build_server(store, client=client)  # type: ignore[arg-type]
    async with create_connected_server_and_client_session(mcp) as session:
        await session.initialize()
        await session.call_tool("list_accounts", {})
    # Give any (incorrect) bg thread a chance to start
    await asyncio.sleep(0.1)
    assert client.fetch_count == 0


@pytest.mark.anyio
async def test_lazy_sync_triggers_once_when_data_is_stale(
    store: DuckDBStore,
) -> None:
    _seed_stale_sync(store, hours_ago=24.0)
    client = _FakeClient()
    mcp = build_server(store, client=client)  # type: ignore[arg-type]
    async with create_connected_server_and_client_session(mcp) as session:
        await session.initialize()
        await session.call_tool("list_accounts", {})
    assert client.fetch_started.wait(timeout=3), "background sync never started"
    assert client.fetch_finished.wait(timeout=3), "background sync never finished"
    assert client.fetch_count == 1


@pytest.mark.anyio
async def test_lazy_sync_lock_prevents_double_trigger(
    store: DuckDBStore,
) -> None:
    _seed_stale_sync(store, hours_ago=24.0)
    client = _FakeClient()
    client.hold()
    mcp = build_server(store, client=client)  # type: ignore[arg-type]
    async with create_connected_server_and_client_session(mcp) as session:
        await session.initialize()
        await session.call_tool("list_accounts", {})
        assert client.fetch_started.wait(timeout=3), "first sync never started"
        # Second tool call while the first sync is held in flight.
        await session.call_tool("list_accounts", {})
    client.release()
    assert client.fetch_finished.wait(timeout=3)
    assert client.fetch_count == 1, (
        f"expected one bg sync under the lock, got {client.fetch_count}"
    )


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"
