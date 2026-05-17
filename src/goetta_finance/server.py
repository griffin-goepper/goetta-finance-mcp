from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any

from mcp.server.fastmcp import FastMCP
from pydantic import Field

from goetta_finance.simplefin import SimpleFinClient
from goetta_finance.store import FinanceStore
from goetta_finance.tools.accounts import list_accounts as _list_accounts
from goetta_finance.tools.balance_history import (
    account_balance_history as _account_balance_history,
)
from goetta_finance.tools.sql_query import sql_query as _sql_query
from goetta_finance.tools.sync_now import sync_now as _sync_now
from goetta_finance.tools.transactions import get_transactions as _get_transactions

SQL_SCHEMA_HINT = """\
Run a read-only SQL query against the local DuckDB store. Only single-statement
SELECT/WITH/EXPLAIN/SHOW/DESCRIBE queries are accepted.

Schema:
  accounts(id, org_id, org_name, name, currency, balance, available_balance,
           balance_date, type, extra, updated_at)
  transactions(id, account_id, posted, transacted_at, amount, description,
               payee, memo, pending, extra, created_at)
  balance_snapshots(account_id, timestamp, balance)
  sync_runs(id, started_at, finished_at, accounts_touched, transactions_new,
            transactions_updated, warnings, errors)

Money columns are DECIMAL(18,2); timestamps are TIMESTAMP in UTC. Transaction
`amount` is signed (negative = money out). Results are well-suited to be
visualized as an inline chart artifact when the user asks for a visualization.
"""


def build_server(
    store: FinanceStore,
    *,
    client: SimpleFinClient | None = None,
    name: str = "goetta-finance",
) -> FastMCP:
    mcp = FastMCP(name)

    @mcp.tool(
        description=(
            "All accounts with current balance. No arguments. Use for 'what "
            "accounts do I have' or 'what's my checking balance'. For deeper "
            "analysis prefer sql_query."
        )
    )
    def list_accounts() -> list[dict[str, Any]]:
        return _list_accounts(store)

    @mcp.tool(
        description=(
            "Get transactions, optionally filtered by account, date range, "
            "or text search across description/payee. For aggregations like "
            "'spending by category' prefer sql_query."
        )
    )
    def get_transactions(
        account_id: Annotated[str | None, Field(description="SimpleFIN account ID.")] = None,
        start: Annotated[
            datetime | None, Field(description="Inclusive UTC start of posted date.")
        ] = None,
        end: Annotated[
            datetime | None, Field(description="Inclusive UTC end of posted date.")
        ] = None,
        search: Annotated[
            str | None,
            Field(description="Case-insensitive substring of description or payee."),
        ] = None,
        limit: Annotated[int, Field(ge=1, le=1000, description="Maximum rows returned.")] = 100,
    ) -> list[dict[str, Any]]:
        return _get_transactions(
            store,
            account_id=account_id,
            start=start,
            end=end,
            search=search,
            limit=limit,
        )

    @mcp.tool(
        description=(
            "Time-series of balance snapshots for a single account. Use this "
            "to chart net worth or detect balance trends."
        )
    )
    def account_balance_history(
        account_id: Annotated[str, Field(description="SimpleFIN account ID.")],
        days: Annotated[int, Field(ge=1, le=3650, description="Lookback window in days.")] = 90,
    ) -> list[dict[str, Any]]:
        return _account_balance_history(store, account_id, days=days)

    @mcp.tool(description=SQL_SCHEMA_HINT)
    def sql_query(
        sql: Annotated[
            str,
            Field(
                description=(
                    "A single read-only SQL statement (SELECT/WITH/EXPLAIN/SHOW/DESCRIBE)."
                )
            ),
        ],
    ) -> list[dict[str, Any]]:
        return _sql_query(store, sql)

    @mcp.tool(
        description=(
            "Trigger a fresh pull from SimpleFIN. Blocks until complete (usually under 30 seconds)."
        )
    )
    def sync_now() -> dict[str, Any]:
        return _sync_now(store, client)

    return mcp
