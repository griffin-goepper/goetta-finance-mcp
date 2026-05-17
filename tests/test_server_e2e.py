from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest
from mcp.shared.memory import create_connected_server_and_client_session

from goetta_finance.models import Account, AccountType, Transaction
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


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"
