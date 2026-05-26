from __future__ import annotations

import logging
import os
from datetime import UTC, datetime
from typing import Annotated, Any

from mcp.server.fastmcp import FastMCP
from pydantic import Field

from goetta_finance.collector import collect_lock, trigger_background_collect
from goetta_finance.simplefin import SimpleFinClient
from goetta_finance.store import FinanceStore
from goetta_finance.tools.accounts import list_accounts as _list_accounts
from goetta_finance.tools.balance_history import (
    account_balance_history as _account_balance_history,
)
from goetta_finance.tools.spending_by_category import (
    spending_by_category as _spending_by_category,
)
from goetta_finance.tools.sql_query import sql_query as _sql_query
from goetta_finance.tools.sync_now import sync_now as _sync_now
from goetta_finance.tools.transactions import get_transactions as _get_transactions

logger = logging.getLogger(__name__)

_LAZY_SYNC_THRESHOLD_HOURS_DEFAULT = 6.0


def _lazy_sync_threshold_hours() -> float:
    """Read the lazy-sync staleness threshold from the environment.

    Override via ``GOETTA_FINANCE_LAZY_SYNC_HOURS``. Default 6h: long enough
    to avoid syncing on every chat turn, short enough that morning balances
    feel current.
    """
    raw = os.environ.get("GOETTA_FINANCE_LAZY_SYNC_HOURS")
    if raw is None:
        return _LAZY_SYNC_THRESHOLD_HOURS_DEFAULT
    try:
        return float(raw)
    except ValueError:
        logger.warning(
            "GOETTA_FINANCE_LAZY_SYNC_HOURS=%r is not a number; using default %s",
            raw,
            _LAZY_SYNC_THRESHOLD_HOURS_DEFAULT,
        )
        return _LAZY_SYNC_THRESHOLD_HOURS_DEFAULT


def _maybe_trigger_lazy_sync(store: FinanceStore, client: SimpleFinClient | None) -> None:
    """Fire-and-forget background sync if data is stale.

    Called from each tool wrapper. Cheap when fresh (one DB query). Does
    nothing when ``client`` is unset (read-only contexts). Uses an OS
    thread so the sync survives the request lifecycle — ``asyncio.create_task``
    would get cancelled when the MCP handler returns.
    """
    if client is None:
        return
    last = store.last_sync_time()
    if last is not None:
        now = datetime.now(tz=UTC)
        age_hours = (now - last).total_seconds() / 3600.0
        if age_hours < _lazy_sync_threshold_hours():
            return
    trigger_background_collect(store, client)


SQL_SCHEMA_HINT = """\
Run a read-only SQL query against the local DuckDB store. Only single-statement
SELECT/WITH/EXPLAIN/SHOW/DESCRIBE queries are accepted.

Schema:
  accounts(id, org_id, org_name, name, currency, balance, available_balance,
           balance_date, type, extra, is_manual, is_liability, updated_at)
  transactions(id, account_id, posted, transacted_at, amount, description,
               payee, memo, pending, extra, created_at)
  balance_snapshots(account_id, timestamp, balance)
  sync_runs(id, started_at, finished_at, accounts_touched, transactions_new,
            transactions_updated, warnings, errors)

Columns is_manual (account was added via CLI, not synced), is_liability
(account represents debt), and is_hidden (account excluded from default
read paths) are user-controlled boolean flags on accounts. All three are
preserved across SimpleFIN syncs — the sync only overwrites SimpleFIN-
sourced columns (balance, available_balance, balance_date, name,
org_name, type, extra). Users flip the flags via `goetta-finance account
set-liability` / `set-hidden`. The flips apply retroactively: net-worth
aggregation, the categorization view (transactions_with_category), and
spending_by_category all join accounts and filter on these flags at
read time, so toggling them changes historical computations without any
backfill.

For net-worth math, use CASE WHEN is_liability THEN -ABS(balance) ELSE
balance END to get the signed contribution per account — a liability
always reduces net worth regardless of how the source signs the balance.
For "what does the user actually want to see" queries, filter
WHERE NOT is_hidden (or COALESCE(is_hidden, FALSE) = FALSE) — the MCP
tools and the dashboard apply this by default; raw sql_query callers
opt in explicitly.

Categorization tables (migration 0004):
  categories(id, name, display_color, is_default)
  category_rules(id, category_id, match_type, pattern, priority, is_default)
  transaction_overrides(transaction_id, category_id, created_at)

Per-transaction category resolves at read time through the
transactions_with_category view, which exposes every transactions column
plus `category`, `category_color`, and `account_is_hidden` (sourced from
the JOINed accounts row — filter on it to exclude transactions belonging
to hidden accounts). Resolution order: if a row in
transaction_overrides exists for the transaction, that override wins;
otherwise the lowest-priority matching rule in category_rules wins
(match_type 'contains' is a case-insensitive substring on description,
match_type 'regex' is a DuckDB regexp_matches call); otherwise the
fallback literal 'Uncategorized' is returned. Rule and override changes
apply retroactively to every existing transaction without backfill —
this is the whole point of read-time resolution; do not write a
category_id column on transactions.

For category-aware queries prefer transactions_with_category over the
bare transactions table. For "what did I spend on X" questions prefer
the spending_by_category tool over ad-hoc SQL; it already enforces the
non-spending-categories-excluded semantic (spending = negative amounts
only, returned as positive dollar values; non-spending categories like
Transfers and Income are excluded by default) and the
include_non_spending opt-in.

The categories table carries an is_spending boolean (default TRUE) for
each category. Transfers and Income are seeded with is_spending=FALSE
by migration 0006 because money moving to your own accounts (Transfers)
or income (Income) isn't spending. Users can add their own non-spending
categories via `goetta-finance category add --no-spending` or toggle
existing ones with `category set-spending <name> <bool>`. The
spending_by_category tool joins categories on the resolved name and
filters WHERE c.is_spending = TRUE by default.

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
            "All accounts with current balance. Hidden accounts (flag "
            "set via `goetta-finance account set-hidden`) are excluded by "
            "default; pass include_hidden=True to see them. Use for 'what "
            "accounts do I have' or 'what's my checking balance'. For "
            "deeper analysis prefer sql_query. Call sync_status if the "
            "user asks whether the data is current."
        )
    )
    def list_accounts(
        include_hidden: Annotated[
            bool,
            Field(
                description=(
                    "When True, include accounts marked is_hidden. Default "
                    "False (matches the dashboard and net-worth math)."
                )
            ),
        ] = False,
    ) -> list[dict[str, Any]]:
        _maybe_trigger_lazy_sync(store, client)
        return _list_accounts(store, include_hidden=include_hidden)

    @mcp.tool(
        description=(
            "Get transactions, optionally filtered by account, date range, "
            "category, or text search across description/payee. Every row "
            "returned carries a resolved `category` field (falling back to "
            "'Uncategorized'). Transactions on hidden accounts are excluded "
            "by default; pass include_hidden=True to include them. For "
            "aggregations like 'spending by category' prefer the "
            "spending_by_category tool."
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
        category: Annotated[
            str | None,
            Field(
                description=(
                    "Filter to transactions resolving to this category "
                    "(case-sensitive; see list_accounts-equivalent "
                    "categories table for canonical names)."
                )
            ),
        ] = None,
        include_hidden: Annotated[
            bool,
            Field(
                description=(
                    "When True, include transactions from accounts marked "
                    "is_hidden. Default False (matches the dashboard)."
                )
            ),
        ] = False,
        search: Annotated[
            str | None,
            Field(description="Case-insensitive substring of description or payee."),
        ] = None,
        limit: Annotated[int, Field(ge=1, le=1000, description="Maximum rows returned.")] = 100,
    ) -> list[dict[str, Any]]:
        _maybe_trigger_lazy_sync(store, client)
        return _get_transactions(
            store,
            account_id=account_id,
            start=start,
            end=end,
            category=category,
            include_hidden=include_hidden,
            search=search,
            limit=limit,
        )

    @mcp.tool(
        description=(
            "Returns categorized spending totals (negative amounts only, "
            "returned as positive dollar values). Non-spending categories "
            "(Transfers, Income, and any category with is_spending=FALSE) "
            "are excluded by default; pass include_non_spending=True to "
            "include them. Income rows come back with a negative total "
            "(cash in); Transfers rows come back positive (cash leaving "
            "the source account, but moving to one of your own accounts)."
        )
    )
    def spending_by_category(
        start: Annotated[datetime, Field(description="Inclusive UTC start of posted date.")],
        end: Annotated[datetime, Field(description="Inclusive UTC end of posted date.")],
        include_non_spending: Annotated[
            bool,
            Field(
                description=(
                    "When True, include categories with is_spending=FALSE "
                    "(Transfers, Income, etc.). Default False — matches the "
                    "dashboard's Spending by category pie."
                )
            ),
        ] = False,
    ) -> list[dict[str, Any]]:
        _maybe_trigger_lazy_sync(store, client)
        return _spending_by_category(store, start, end, include_non_spending=include_non_spending)

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
        _maybe_trigger_lazy_sync(store, client)
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
        _maybe_trigger_lazy_sync(store, client)
        return _sql_query(store, sql)

    @mcp.tool(
        description=(
            "Report when the SimpleFIN data was last synced. Call this when "
            "the user asks 'is this current?', 'when was the last sync?', or "
            "anytime the freshness of a balance/transaction matters. Returns "
            "the absolute timestamp, the age in hours, whether a background "
            "sync is in progress right now, and the configured staleness "
            "threshold. Other tools automatically trigger a background sync "
            "when data is older than that threshold."
        )
    )
    def sync_status() -> dict[str, Any]:
        last = store.last_sync_time()
        threshold = _lazy_sync_threshold_hours()
        now = datetime.now(tz=UTC)
        age_hours: float | None = (
            None if last is None else round((now - last).total_seconds() / 3600.0, 2)
        )
        # Probe lock without taking it. If we can acquire non-blocking, no
        # sync is running; release immediately. If not, one is.
        acquired = collect_lock.acquire(blocking=False)
        sync_in_progress = not acquired
        if acquired:
            collect_lock.release()
        return {
            "last_sync_iso": last.isoformat() if last else None,
            "data_age_hours": age_hours,
            "stale": age_hours is None or age_hours >= threshold,
            "staleness_threshold_hours": threshold,
            "sync_in_progress": sync_in_progress,
        }

    @mcp.tool(
        description=(
            "Trigger a fresh pull from SimpleFIN. Blocks until complete (usually under 30 seconds). "
            "If a background sync is already running, this returns immediately with skipped=true."
        )
    )
    def sync_now() -> dict[str, Any]:
        return _sync_now(store, client)

    return mcp
