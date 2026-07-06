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
            "spending_by_category",
            "categorize_transaction",
            "uncategorize_transaction",
            "add_category_rule",
            "remove_category_rule",
            "top_uncategorized_patterns",
            "list_goals",
            "set_goal",
            "remove_goal",
        }


@pytest.mark.anyio
async def test_server_spending_by_category_e2e(store: DuckDBStore) -> None:
    """Full MCP round-trip: seed transactions, call the tool through the
    client session, decode the structured result, assert shape + sort."""
    store.upsert_accounts(
        [
            Account(
                id="e2e-a1",
                org_name="Test",
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
                id="e2e-sbux",
                account_id="e2e-a1",
                posted=datetime(2026, 5, 5, tzinfo=UTC),
                amount=Decimal("-12.50"),
                description="STARBUCKS STORE #1",
            ),
            Transaction(
                id="e2e-kroger",
                account_id="e2e-a1",
                posted=datetime(2026, 5, 10, tzinfo=UTC),
                amount=Decimal("-87.45"),
                description="KROGER #999",
            ),
        ]
    )
    mcp = build_server(store)
    async with create_connected_server_and_client_session(mcp) as session:
        await session.initialize()
        result = await session.call_tool(
            "spending_by_category",
            {
                "start": "2026-05-01T00:00:00Z",
                "end": "2026-05-31T23:59:59Z",
            },
        )
    payload = _decode(result)
    assert isinstance(payload, list)
    cats = [r["category"] for r in payload]
    assert "Dining" in cats
    assert "Groceries" in cats
    # Sorted descending by total: Groceries (87.45) > Dining (12.50).
    by_cat = {r["category"]: r for r in payload}
    assert Decimal(by_cat["Groceries"]["total"]) > Decimal(by_cat["Dining"]["total"])


@pytest.mark.anyio
async def test_server_curation_tools_e2e(store: DuckDBStore) -> None:
    """Full MCP round-trip for the curation surface: surface an
    uncategorized pattern → add a rule → verify resolution; then
    override one transaction and clear it; finally remove the rule."""
    store.upsert_accounts(
        [
            Account(
                id="cur-e2e",
                org_name="Test",
                name="Checking",
                balance=Decimal("100.00"),
                balance_date=datetime(2026, 5, 1, tzinfo=UTC),
                type=AccountType.CHECKING,
            )
        ]
    )
    now = datetime.now(tz=UTC)
    store.upsert_transactions(
        [
            Transaction(
                id="cur-e2e-t1",
                account_id="cur-e2e",
                posted=now - timedelta(days=2),
                amount=Decimal("-45.00"),
                description="NEW GYM LLC MEMBERSHIP",
            )
        ]
    )
    mcp = build_server(store)
    async with create_connected_server_and_client_session(mcp) as session:
        await session.initialize()

        # 1. Discovery: the gym shows up as uncategorized.
        result = await session.call_tool("top_uncategorized_patterns", {"days": 30, "top": 5})
        payload = _decode(result)
        assert isinstance(payload, list)
        assert any("NEW GYM" in r["pattern"] for r in payload)

        # 2. Curation: add a rule through MCP.
        result = await session.call_tool(
            "add_category_rule",
            {"category": "Healthcare", "pattern": "NEW GYM LLC"},
        )
        payload = _decode(result)
        assert payload["ok"] is True
        rule_id = payload["rule_id"]

        # 3. The transaction now resolves through the rule.
        result = await session.call_tool("get_transactions", {"search": "GYM"})
        payload = _decode(result)
        assert payload[0]["category"] == "Healthcare"

        # 4. Override beats the rule; clearing falls back.
        result = await session.call_tool(
            "categorize_transaction",
            {"transaction_id": "cur-e2e-t1", "category": "Entertainment"},
        )
        assert _decode(result)["ok"] is True
        result = await session.call_tool("get_transactions", {"search": "GYM"})
        assert _decode(result)[0]["category"] == "Entertainment"

        result = await session.call_tool(
            "uncategorize_transaction", {"transaction_id": "cur-e2e-t1"}
        )
        assert _decode(result)["ok"] is True
        result = await session.call_tool("get_transactions", {"search": "GYM"})
        assert _decode(result)[0]["category"] == "Healthcare"

        # 5. Removing the rule un-categorizes the transaction again.
        result = await session.call_tool("remove_category_rule", {"rule_id": rule_id})
        assert _decode(result)["ok"] is True
        result = await session.call_tool("get_transactions", {"search": "GYM"})
        assert _decode(result)[0]["category"] == "Uncategorized"


@pytest.mark.anyio
async def test_server_goal_tools_e2e(store: DuckDBStore) -> None:
    """Full MCP round-trip for the goals surface: create a cap and a
    balance goal → list with computed progress → remove → gone."""
    store.upsert_accounts(
        [
            Account(
                id="goal-e2e",
                org_name="Test",
                name="Savings",
                balance=Decimal("6500.00"),
                balance_date=datetime.now(tz=UTC),
                type=AccountType.SAVINGS,
            )
        ]
    )
    store.upsert_transactions(
        [
            Transaction(
                id="goal-e2e-t1",
                account_id="goal-e2e",
                posted=datetime.now(tz=UTC),
                amount=Decimal("-450.00"),
                description="GOAL E2E SPEND",
            )
        ]
    )
    store.set_transaction_override("goal-e2e-t1", "Dining")
    mcp = build_server(store)
    async with create_connected_server_and_client_session(mcp) as session:
        await session.initialize()

        result = await session.call_tool(
            "set_goal",
            {
                "name": "Dining cap",
                "kind": "spending_cap",
                "amount": 400.00,
                "category": "dining",  # case-insensitive
                "period": "month",
            },
        )
        payload = _decode(result)
        assert payload["ok"] is True, payload
        cap_id = payload["goal_id"]

        result = await session.call_tool(
            "set_goal",
            {
                "name": "Emergency fund",
                "kind": "balance",
                "amount": 10000,
                "account_id": "goal-e2e",
                "direction": "at_least",
                "target_date": "2999-01-01",
            },
        )
        assert _decode(result)["ok"] is True

        result = await session.call_tool("list_goals", {})
        goals = _decode(result)
        assert isinstance(goals, list)
        by_name = {g["name"]: g for g in goals}
        cap = by_name["Dining cap"]
        assert cap["status"] == "over"
        assert cap["current"] == "450.00"
        assert cap["target"] == "400.00"
        assert isinstance(cap["percent"], str)
        assert cap["period_start"].endswith("00:00:00+00:00")
        fund = by_name["Emergency fund"]
        assert fund["status"] == "on_track"
        assert fund["current"] == "6500.00"
        assert fund["direction"] == "at_least"
        assert fund["target_date"] == "2999-01-01"

        # Money fields are strings, never JSON numbers.
        assert all(isinstance(g["amount"], str) for g in goals)

        result = await session.call_tool("remove_goal", {"goal_id": cap_id})
        assert _decode(result)["ok"] is True
        result = await session.call_tool("list_goals", {})
        assert [g["name"] for g in _decode(result)] == ["Emergency fund"]


@pytest.mark.anyio
async def test_server_set_goal_rejects_bad_shape_e2e(store: DuckDBStore) -> None:
    """The MCP write surface refuses cross-field shape violations —
    same validator as the CLI, through the real FastMCP path."""
    mcp = build_server(store)
    async with create_connected_server_and_client_session(mcp) as session:
        await session.initialize()
        result = await session.call_tool(
            "set_goal",
            {"name": "shapeless", "kind": "spending_cap", "amount": 100},
        )
        payload = _decode(result)
        assert payload["ok"] is False
        assert "validation failed" in payload["error"]

        result = await session.call_tool(
            "set_goal",
            {
                "name": "typo cap",
                "kind": "spending_cap",
                "amount": 100,
                "category": "Gorceries",
                "period": "month",
            },
        )
        payload = _decode(result)
        assert payload["ok"] is False
        assert "category not found" in payload["error"]
        assert "Did you mean" in payload["error"]


@pytest.mark.anyio
async def test_server_add_category_rule_rejects_redos_e2e(store: DuckDBStore) -> None:
    """The MCP write surface refuses ReDoS patterns — same validator as
    the CLI, exercised through the real FastMCP parameter path."""
    mcp = build_server(store)
    async with create_connected_server_and_client_session(mcp) as session:
        await session.initialize()
        result = await session.call_tool(
            "add_category_rule",
            {"category": "Dining", "pattern": "(a+)+$", "match_type": "regex"},
        )
        payload = _decode(result)
        assert payload["ok"] is False
        assert "validation failed" in payload["error"]


@pytest.mark.anyio
async def test_server_add_category_rule_with_bounds_e2e(store: DuckDBStore) -> None:
    """Amount bounds through the real FastMCP parameter path: pins the
    nullable float Field shape (no gt= constraint — pydantic v2 can't
    always apply constraints to nullable schemas) and the float→Decimal
    wire-boundary conversion."""
    mcp = build_server(store)
    async with create_connected_server_and_client_session(mcp) as session:
        await session.initialize()
        result = await session.call_tool(
            "add_category_rule",
            {"category": "Dining", "pattern": "ZZZ-SPEEDY", "max_amount": 20},
        )
        payload = _decode(result)
        assert payload["ok"] is True, payload
        row = store.conn.execute(
            "SELECT min_amount, max_amount FROM category_rules WHERE id = ?",
            [payload["rule_id"]],
        ).fetchone()
        assert row is not None
        assert row[0] is None
        assert row[1] == Decimal("20.00")

        # Inverted bounds refused through the same path — shared validator.
        result = await session.call_tool(
            "add_category_rule",
            {
                "category": "Dining",
                "pattern": "ZZZ-SPEEDY-2",
                "min_amount": 30,
                "max_amount": 20,
            },
        )
        payload = _decode(result)
        assert payload["ok"] is False
        assert "validation failed" in payload["error"]


def test_schema_hint_mentions_categorization_tables() -> None:
    """Floor: identifier markers present. If a future schema slice
    adds a table or flag, this test fails until the hint is updated."""
    from goetta_finance.server import SQL_SCHEMA_HINT

    for marker in (
        "is_manual",
        "is_liability",
        "is_hidden",
        "is_spending",
        "categories",
        "category_rules",
        "transaction_overrides",
        "transactions_with_category",
        "account_is_hidden",
        "goals",
        "target_date",
        "min_amount",
        "max_amount",
    ):
        assert marker in SQL_SCHEMA_HINT, f"SQL_SCHEMA_HINT missing {marker!r}"


def test_schema_hint_communicates_categorization_semantics() -> None:
    """Ceiling: load-bearing phrases present. The identifier-only test
    above catches a missing table name, but it doesn't catch a rewrite
    that keeps the names while losing the *meaning* (e.g. drops the
    retroactivity property, or fails to point Claude at
    spending_by_category over ad-hoc SQL). These phrases are what the
    hint is actually supposed to communicate."""
    from goetta_finance.server import SQL_SCHEMA_HINT

    expected_phrases = [
        "read time",  # retroactivity property
        "transaction_overrides",  # override-beats-rule resolution
        "Uncategorized",  # fallback default
        "spending_by_category",  # tool-preference guidance
        "preserved across",  # user-owned flag preservation guarantee (0005)
        "non-spending",  # is_spending semantic guarantee (0006)
        "top_uncategorized_patterns",  # curation discovery entry point
        "add_category_rule",  # curation write path (NOT sql_query)
        "remove_category_rule",  # rule deletion path (defaults → CLI)
        "categorize_transaction",  # one-off override path
        "list_goals",  # goal read path carries computed status/pace
        "set_goal",  # goal write path (NOT sql_query)
        "at_most",  # balance-goal direction semantics
        "amount owed",  # liability abs rule
        "absolute value",  # amount bounds compare against abs(amount) (0009)
        "exclusive",  # max bound is exclusive — half-open interval (0009)
    ]
    for phrase in expected_phrases:
        assert phrase in SQL_SCHEMA_HINT, (
            f"SQL_SCHEMA_HINT missing semantic phrase: {phrase!r}. The "
            "identifier names alone don't tell Claude how to use the "
            "view; please re-check the categorization paragraph."
        )


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

    def fetch_chunked(self, start: datetime, end: datetime, chunk_days: int = 60) -> Any:
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
    assert client.fetch_count == 1, f"expected one bg sync under the lock, got {client.fetch_count}"


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"
