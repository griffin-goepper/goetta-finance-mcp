from __future__ import annotations

import logging
import os
from datetime import UTC, datetime
from decimal import Decimal
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
from goetta_finance.tools.categorize import (
    add_category_rule as _add_category_rule,
)
from goetta_finance.tools.categorize import (
    categorize_transaction as _categorize_transaction,
)
from goetta_finance.tools.categorize import (
    remove_category_rule as _remove_category_rule,
)
from goetta_finance.tools.categorize import (
    uncategorize_transaction as _uncategorize_transaction,
)
from goetta_finance.tools.goals import list_goals as _list_goals
from goetta_finance.tools.goals import remove_goal as _remove_goal
from goetta_finance.tools.goals import set_goal as _set_goal
from goetta_finance.tools.set_account_balance import (
    set_account_balance as _set_account_balance,
)
from goetta_finance.tools.spending_by_category import (
    spending_by_category as _spending_by_category,
)
from goetta_finance.tools.sql_query import sql_query as _sql_query
from goetta_finance.tools.sync_now import sync_now as _sync_now
from goetta_finance.tools.transactions import get_transactions as _get_transactions
from goetta_finance.tools.transfer_links import (
    link_account_transfers as _link_account_transfers,
)
from goetta_finance.tools.transfer_links import (
    list_transfer_links as _list_transfer_links,
)
from goetta_finance.tools.transfer_links import (
    unlink_account_transfers as _unlink_account_transfers,
)
from goetta_finance.tools.uncategorized import (
    top_uncategorized_patterns as _top_uncategorized_patterns,
)

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
  goals(id, name, kind, amount, category_id, period, account_id, direction,
        target_date, match_type, match_pattern, baseline_amount, baseline_date,
        recurring_amount, recurring_interval, recurring_anchor, created_at)
  transfer_links(id, account_id, source_account_id, match_type, pattern,
                 anchor, created_at)
  transfer_link_applications(transaction_id, account_id, link_id, amount,
                             posted, applied_at)

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

The transactions `pending` flag marks the bank's in-flight snapshot, not
history: each sync replaces the pending set with what the feed currently
reports, so a pending row can disappear (hold released) or settle. Banks
may reissue the id when a pending transaction settles — the pending row
vanishes and a NEW posted row appears — and `posted` on a pending row
may be the transaction time, not the settlement time, so a transaction
can shift between month buckets when it settles. Aggregation semantics:
spending caps, spending_by_category, and the by-month matrix all COUNT
pending rows (pending charges are committed money — early warning beats
precision); the dashboard's monthly income/spending bars EXCLUDE them;
transfer roll-forward waits for settlement. Because pending ids are
unstable, prefer add_category_rule over categorize_transaction for
pending rows — a rule matches the settled row no matter what id it
arrives under, while a per-id override is dropped if the id changes.

Categorization tables (migrations 0004/0013):
  categories(id, name, display_color, is_default)
  category_rules(id, category_id, match_type, pattern, priority, is_default,
                 min_amount, max_amount)
  transaction_overrides(transaction_id, category_id, created_at)
  category_match_cache(transaction_id, category_id)

Per-transaction category resolves through the
transactions_with_category view, which exposes every transactions column
plus `category`, `category_color`, and `account_is_hidden` (sourced from
the JOINed accounts row — filter on it to exclude transactions belonging
to hidden accounts). Resolution order: if a row in
transaction_overrides exists for the transaction, that override wins;
otherwise the transaction's matched rule wins
(match_type 'contains' is a case-insensitive substring on description,
match_type 'regex' is a DuckDB regexp_matches call; the lowest-priority
matching rule is the match); otherwise the
fallback literal 'Uncategorized' is returned. Rules may carry optional
min_amount/max_amount bounds compared against the absolute value of the
transaction amount (a rule matches when abs(amount) >= min_amount AND
abs(amount) < max_amount; NULL = unbounded on that side). Bounds only
refine a pattern match — a rule never matches on amount alone — and the
max bound is exclusive, so complementary rules at the same threshold
(e.g. SPEEDWAY under/over 20) have no gap or overlap. Rule and override
changes apply retroactively to every existing transaction without
manual backfill; do not write a category_id column on transactions.
category_match_cache is derived bookkeeping behind that contract: it
holds each transaction's matched rule's category_id and is rebuilt
automatically inside every rule/transaction write, so treat it as an
implementation detail — query the view, never the cache, for category
answers.

For category-aware queries prefer transactions_with_category over the
bare transactions table. For "what did I spend on X" questions prefer
the spending_by_category tool over ad-hoc SQL; it already enforces the
non-spending-categories-excluded semantic (spending = negative amounts
only, returned as positive amounts; non-spending categories like
Transfers and Income are excluded by default) and the
include_non_spending opt-in.

Categorization curation happens in conversation: call
top_uncategorized_patterns to surface what's hiding in the
Uncategorized bucket, then categorize_transaction (one-off override)
or add_category_rule (retroactive class-of-transactions rule) to act
on it. remove_category_rule deletes a user rule by id, equally
retroactively (default seeded rules are refused — those go through
the CLI's confirmed --force path). Don't write INSERTs via sql_query
— it's read-only; the curation tools are the write path and they run
the same pattern validation as the CLI.

The categories table carries an is_spending boolean (default TRUE) for
each category. Transfers and Income are seeded with is_spending=FALSE
by migration 0006 because money moving to your own accounts (Transfers)
or income (Income) isn't spending. Users can add their own non-spending
categories via `goetta-finance category add --no-spending` or toggle
existing ones with `category set-spending <name> <bool>`. The
spending_by_category tool joins categories on the resolved name and
filters WHERE c.is_spending = TRUE by default.

The goals table (migrations 0008/0014) holds user-defined thresholds
with a kind discriminator: 'spending_cap' rows set category_id + period
('month'|'year'), 'balance' rows set account_id + direction
('at_least'|'at_most') and an optional target_date, 'contribution' rows
set account_id + period plus optional match_type/match_pattern and
baseline_amount/baseline_date (each pair travels together, contribution
only). Goal status and progress are NOT stored — they are computed at
read time (the same retroactivity property as the categorization view:
recategorizing a transaction changes goal progress with no backfill).
Spending caps use the same net-spending semantics as
spending_by_category over UTC calendar buckets. Balance goals on
is_liability accounts evaluate the absolute balance (amount owed):
at_most is a debt ceiling, at_least a savings floor. Contribution goals
("contribute at least $X into account Y per month/year") count money
INTO the goal's own account per UTC calendar bucket: the
absolute value of matched SETTLED transactions on the account (pattern
against description OR payee, same contains/regex semantics as transfer
links; abs() because brokerages often sign cash-in negative), plus the
account's transfer_link_applications rows posted in the period (already
signed money-in — a linked manual account needs no pattern), plus
baseline_amount when baseline_date falls in the period (contributions
made before the feed's history), plus DECLARED recurring accrual:
recurring_amount / recurring_interval ('weekly'|'biweekly'|'monthly') /
recurring_anchor declare a schedule (payroll deductions no feed can
see) and each elapsed payday accrues recurring_amount by calculation —
declared, not observed. The payday series extends both directions from
the anchor; monthly uses the anchor's day-of-month,
clamped to the month end. The recurring triple travels all-or-none and only on
contribution rows, enforced in the application layer (0015 is plain
ALTERs; DuckDB can't ALTER a table CHECK in) — raw SQL won't stop you
writing an inconsistent triple, so goal writes MUST go through
set_goal. Ahead of the funding clock is on_track
for contributions — the inverse of caps, and a goal whose schedule
alone covers the target stays on_track between paydays. Prefer the
list_goals tool over ad-hoc SQL on this table — it carries the
computed status, pace, and projection fields. Goal writes go through
set_goal / remove_goal, not sql_query.

The transfer_links table (migration 0012) connects a manual account to
matching transactions on a synced account (pattern against payee OR
description) so its balance rolls FORWARD automatically: each sync — and
each link creation — applies matched settled transactions posted after
the link's anchor through the same write path as `account set-balance`
(accounts.balance plus a balance_snapshots row), and records them in
transfer_link_applications so nothing ever double-counts. This is
write-time bookkeeping, not read-time resolution: balances and
snapshots stay authoritative, and a `set-balance` true-up (the
set_account_balance tool, or `goetta-finance account set-balance`)
re-anchors the links (that's also how interest gets captured — transfer
sums can't see it). Prefer the list_transfer_links /
link_account_transfers / unlink_account_transfers tools over ad-hoc SQL
on these tables.

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
            "Returns NET spending totals per category (spending minus "
            "refunds), as positive dollar values. A refund within a "
            "spending category (a positive amount, e.g. a returned Dining "
            "purchase) reduces that category's total. A positive amount in "
            "Uncategorized contributes 0 (ambiguous until categorized). "
            "Non-spending categories (Transfers, Income, and any category "
            "with is_spending=FALSE) are excluded by default; pass "
            "include_non_spending=True to include them — Income rows come "
            "back with a negative total (cash in), Transfers positive "
            "(cash leaving the source account, but moving to one of your "
            "own accounts)."
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

    @mcp.tool(
        description=(
            "Apply a manual per-transaction category override. The override "
            "beats any matching rule and applies immediately to all reads. "
            "Use for one-off recategorizations ('this transaction is actually "
            "rent, not shopping'). Category names are case-insensitive; on a "
            "typo the error suggests the closest match. For categorizing a "
            "CLASS of transactions (every future occurrence of a merchant), "
            "prefer add_category_rule instead."
        )
    )
    def categorize_transaction(
        transaction_id: Annotated[
            str, Field(description="Transaction id (from get_transactions).")
        ],
        category: Annotated[
            str, Field(description="Category name (case-insensitive, e.g. 'Dining').")
        ],
    ) -> dict[str, Any]:
        return _categorize_transaction(store, transaction_id, category)

    @mcp.tool(
        description=(
            "Remove a per-transaction category override. Idempotent — safe to "
            "call even if no override exists. After clearing, the transaction "
            "resolves through rules again (or falls back to 'Uncategorized'). "
            "Use when the user wants to undo a categorize_transaction call."
        )
    )
    def uncategorize_transaction(
        transaction_id: Annotated[
            str, Field(description="Transaction id to clear the override on.")
        ],
    ) -> dict[str, Any]:
        return _uncategorize_transaction(store, transaction_id)

    @mcp.tool(
        description=(
            "Add a categorization rule that applies retroactively to every "
            "matching transaction — past and future — with no backfill needed. "
            "match_type 'contains' is a case-insensitive substring on the "
            "transaction description; 'regex' is a DuckDB regexp_matches call. "
            "Lower priority wins when multiple rules match (default 100; "
            "transfer-like patterns typically use 5 so they beat spending "
            "rules). Patterns are validated for ReDoS shapes before insert. "
            "Optional min_amount/max_amount bounds refine a match by the "
            "ABSOLUTE transaction amount (min inclusive, max exclusive): "
            "dual-use merchants split cleanly, e.g. pattern 'SPEEDWAY' with "
            "max_amount 20 catches gas-station snacks while a complementary "
            "min_amount 20 rule catches fuel fills — no gap or overlap at "
            "exactly 20.00. Bounds never match on their own; pattern is "
            "always required. Use when the user asks to categorize a class "
            "of transactions ('from now on, anything from Duke Energy is "
            "Utilities'). For one-off fixes prefer categorize_transaction."
        )
    )
    def add_category_rule(
        category: Annotated[
            str, Field(description="Category name (case-insensitive, e.g. 'Utilities').")
        ],
        pattern: Annotated[
            str,
            Field(description="Pattern matched against transaction description."),
        ],
        match_type: Annotated[
            str, Field(description="'contains' (default) or 'regex'.")
        ] = "contains",
        priority: Annotated[
            int,
            Field(
                ge=1,
                le=1000,
                description="Lower number = higher precedence. Default 100.",
            ),
        ] = 100,
        min_amount: Annotated[
            float | None,
            Field(
                description="Optional: only match when abs(amount) >= this "
                "(inclusive). Refines the pattern — never matches on amount alone."
            ),
        ] = None,
        max_amount: Annotated[
            float | None,
            Field(
                description="Optional: only match when abs(amount) < this "
                "(exclusive, half-open). E.g. 20 for 'under $20'."
            ),
        ] = None,
    ) -> dict[str, Any]:
        # Same wire-boundary rationale as set_goal: str() gives the exact
        # shortest repr, so sub-cent floats reach the shared validator and
        # fail with a friendly error instead of being silently rounded.
        return _add_category_rule(
            store,
            category,
            match_type,
            pattern,
            priority,
            min_amount=Decimal(str(min_amount)) if min_amount is not None else None,
            max_amount=Decimal(str(max_amount)) if max_amount is not None else None,
        )

    @mcp.tool(
        description=(
            "Remove a categorization rule by id. Retroactive like "
            "add_category_rule: transactions the rule matched immediately "
            "resolve through the remaining rules (or fall back to "
            "'Uncategorized') — no backfill. Find rule ids via sql_query on "
            "category_rules (join categories for the category name). Confirm "
            "with the user before deleting — rules are user-authored "
            "configuration, and tell them what still matches afterwards if "
            "another rule covers the same transactions. Default (seeded, "
            "is_default=TRUE) rules are refused; removing those requires the "
            "CLI's typed-confirmation path "
            "(goetta-finance category remove-rule <id> --force)."
        )
    )
    def remove_category_rule(
        rule_id: Annotated[
            int,
            Field(ge=1, description="Rule id (from sql_query on category_rules)."),
        ],
    ) -> dict[str, Any]:
        return _remove_category_rule(store, rule_id)

    @mcp.tool(
        description=(
            "Surface the largest spending patterns currently sitting in the "
            "Uncategorized bucket, normalized by stripping bank/processor "
            "description prefixes (configurable via prefixes.txt) and grouping "
            "by the first two tokens of what remains. Sorted by total "
            "descending. Use when the user asks 'what's still uncategorized?' "
            "or 'what should I add a rule for next?'. Each row carries a "
            "suggested CLI command; alternatively call add_category_rule "
            "directly once the user picks a category for a pattern."
        )
    )
    def top_uncategorized_patterns(
        days: Annotated[int, Field(ge=1, le=3650, description="Lookback window in days.")] = 30,
        top: Annotated[int, Field(ge=1, le=100, description="Maximum rows returned.")] = 10,
    ) -> list[dict[str, Any]]:
        _maybe_trigger_lazy_sync(store, client)
        return _top_uncategorized_patterns(store, days=days, top=top)

    @mcp.tool(
        description=(
            "List the user's goals with progress, status, and pace computed "
            "fresh at call time. Spending caps report net spending in the "
            "category this calendar month/year (UTC buckets, same math as "
            "spending_by_category — refunds reduce the total, hidden "
            "accounts excluded, pending transactions count) versus the cap, "
            "plus percent of the period elapsed for pace comparison. "
            "Balance goals report the account's current balance versus the "
            "target — for liability accounts the ABSOLUTE balance is used, "
            "so direction 'at_most' 2000 on a credit card means 'owe under "
            "2000' regardless of sign convention. Balance goals also carry "
            "monthly_delta (average movement toward the goal from the last "
            "90 days of history; positive = approaching), projected_date "
            "(trend extrapolation), required_monthly (when a "
            "target_date is set), and pending_delta (sum of still-pending "
            "linked transfers, counted into the balance when they settle; "
            "positive = approaching; null when the account has no transfer "
            "links). Contribution goals report money contributed INTO the "
            "account this calendar month/year: the ABSOLUTE value of settled "
            "transactions matching the goal's pattern (brokerages often sign "
            "cash-in negative) plus applied linked transfers plus any "
            "baseline plus DECLARED recurring accrual (recurring_amount per "
            "elapsed scheduled payday — calculated from the declared "
            "schedule, never observed in a feed; disclose that to the user "
            "when reporting progress), with pending matches previewed in "
            "pending_delta and required_monthly on unmet year goals (net of "
            "future scheduled paydays); being ahead of the clock "
            "is on_track (the inverse of caps). status is one of on_track / "
            "at_risk / over / met, evaluated at read time — recategorizing "
            "transactions retroactively changes progress. Use when the "
            "user asks about budgets, caps, savings targets, debt "
            "paydown, or contribution/funding pace (IRA, HSA, savings). "
            "To create or delete goals use set_goal / remove_goal."
        )
    )
    def list_goals() -> list[dict[str, Any]]:
        _maybe_trigger_lazy_sync(store, client)
        return _list_goals(store)

    @mcp.tool(
        description=(
            "Create a goal. kind='spending_cap' requires category + period "
            "('month' or 'year'): net spending in that category should stay "
            "under amount per calendar period. kind='balance' requires "
            "account_id (from list_accounts) + direction: 'at_least' for "
            "savings targets and emergency-fund floors, 'at_most' for debt "
            "ceilings/paydown (liability accounts evaluate the absolute "
            "balance = amount owed); target_date (YYYY-MM-DD, future) is "
            "optional and enables required-per-month pace tracking. "
            "kind='contribution' requires account_id + period: contribute at "
            "least amount INTO that account per calendar period, counted "
            "from the account's own data. Synced accounts need "
            "match_pattern (matched against description OR payee; matched "
            "amounts count by ABSOLUTE value since brokerages often sign "
            "cash-in negative) — e.g. a Roth IRA: kind='contribution', "
            "amount=7500, period='year', match_pattern='CASH CONTRIBUTION "
            "CURRENT YEAR', match_type='contains'. Manual accounts fed by "
            "transfer links need no pattern — the applied-transfer ledger "
            "already counts money in. baseline_amount + baseline_date "
            "(ISO, not future) credit contributions made before the feed's "
            "history to the period containing that date. "
            "recurring_amount + recurring_interval ('weekly'|'biweekly'|"
            "'monthly', default 'biweekly') + recurring_anchor (ISO date, "
            "past OK) declare a recurring contribution NO feed can observe "
            "— e.g. an HSA funded by payroll: recurring_amount=150.00, "
            "recurring_interval='biweekly', recurring_anchor='2026-01-09' "
            "accrues 150.00 per payday by calculation. This is DECLARED, "
            "not observed — progress assumes the schedule actually "
            "happens, so tell the user that when reporting it. amount "
            "must be positive, at most two decimal places. name must be "
            "unique. Errors return {ok: false, error} with a did-you-mean "
            "suggestion for category typos — fix and retry. Confirm the "
            "amount and shape with the user before creating."
        )
    )
    def set_goal(
        name: Annotated[
            str, Field(description="Unique goal name, e.g. 'Groceries cap' or 'Emergency fund'.")
        ],
        kind: Annotated[str, Field(description="'spending_cap' or 'balance'.")],
        amount: Annotated[float, Field(gt=0, description="Threshold amount, e.g. 400 or 400.00.")],
        category: Annotated[
            str | None,
            Field(description="Spending caps only: category name (case-insensitive)."),
        ] = None,
        period: Annotated[
            str | None, Field(description="Spending caps only: 'month' or 'year'.")
        ] = None,
        account_id: Annotated[
            str | None,
            Field(description="Balance goals only: account id (from list_accounts)."),
        ] = None,
        direction: Annotated[
            str | None, Field(description="Balance goals only: 'at_least' or 'at_most'.")
        ] = None,
        target_date: Annotated[
            str | None,
            Field(description="Balance goals only: optional future deadline, YYYY-MM-DD."),
        ] = None,
        match_type: Annotated[
            str | None,
            Field(
                description=(
                    "Contribution goals only: 'contains' (default when "
                    "match_pattern is given) or 'regex'."
                )
            ),
        ] = None,
        match_pattern: Annotated[
            str | None,
            Field(
                description=(
                    "Contribution goals only: pattern matched against the "
                    "account's own transaction description OR payee; matched "
                    "amounts count by absolute value. Required for synced "
                    "accounts, optional for manual accounts with transfer links."
                )
            ),
        ] = None,
        baseline_amount: Annotated[
            float | None,
            Field(
                description=(
                    "Contribution goals only: contributions already made "
                    "before the feed's history; requires baseline_date."
                )
            ),
        ] = None,
        baseline_date: Annotated[
            str | None,
            Field(
                description=(
                    "Contribution goals only: ISO date the baseline was "
                    "reached (not future); counted into the period "
                    "containing it. Requires baseline_amount."
                )
            ),
        ] = None,
        recurring_amount: Annotated[
            float | None,
            Field(
                description=(
                    "Contribution goals only: DECLARED amount accrued per "
                    "scheduled payday (e.g. a payroll deduction no feed "
                    "sees); requires recurring_anchor."
                )
            ),
        ] = None,
        recurring_interval: Annotated[
            str | None,
            Field(
                description=(
                    "Contribution goals only: 'weekly', 'biweekly' (default "
                    "when recurring_amount is given), or 'monthly' (anchor's "
                    "day-of-month, clamped to month end)."
                )
            ),
        ] = None,
        recurring_anchor: Annotated[
            str | None,
            Field(
                description=(
                    "Contribution goals only: ISO date of any payday in the "
                    "schedule (past OK — the series extends both directions). "
                    "Requires recurring_amount."
                )
            ),
        ] = None,
    ) -> dict[str, Any]:
        # The only float in goal math is this wire boundary. str() gives
        # the shortest round-trip repr, so two-decimal JSON inputs convert
        # exactly; sub-cent inputs reach the shared validator and are
        # rejected with a friendly error rather than silently rounded —
        # keeps this surface gated identically to the CLI.
        return _set_goal(
            store,
            name=name,
            kind=kind,
            amount=Decimal(str(amount)),
            category=category,
            period=period,
            account_id=account_id,
            direction=direction,
            target_date=target_date,
            match_type=match_type,
            match_pattern=match_pattern,
            baseline_amount=Decimal(str(baseline_amount)) if baseline_amount is not None else None,
            baseline_date=baseline_date,
            recurring_amount=Decimal(str(recurring_amount))
            if recurring_amount is not None
            else None,
            recurring_interval=recurring_interval,
            recurring_anchor=recurring_anchor,
        )

    @mcp.tool(
        description=(
            "Delete a goal by id (get ids from list_goals). Unknown ids "
            "return {ok: false, error}. Confirm with the user before "
            "deleting — goals are user-authored configuration."
        )
    )
    def remove_goal(
        goal_id: Annotated[int, Field(ge=1, description="Goal id from list_goals.")],
    ) -> dict[str, Any]:
        return _remove_goal(store, goal_id)

    @mcp.tool(
        description=(
            "Transfer links (manual-account roll-forward config) plus detected "
            "candidates. A link connects a manual account to matching "
            "transactions on a synced account so its balance rolls forward "
            "automatically on every sync — e.g. checking debits with payee "
            "'Apple Savings' credit the manual 'Apple Savings' account. Each "
            "suggestion is a synced account whose settled transactions carry a "
            "payee exactly matching a linkless manual account's name (seen 2+ "
            "times), with counts, totals, and how much would roll forward "
            "immediately on linking. Suggestions are never auto-applied: offer "
            "them to the user and call link_account_transfers once they "
            "confirm. Use when the user asks why a manual balance looks "
            "stale, or about tracking contributions to a savings account."
        )
    )
    def list_transfer_links() -> dict[str, Any]:
        _maybe_trigger_lazy_sync(store, client)
        return _list_transfer_links(store)

    @mcp.tool(
        description=(
            "Link a manual account to matching transfers on a synced account. "
            "account_id must be a manual, non-liability account (MANUAL-...); "
            "source_account_id a synced one (from list_accounts); pattern is "
            "matched against transaction payee and description (match_type "
            "'contains' = case-insensitive substring, or 'regex'). On success "
            "the link immediately applies matched settled transactions posted "
            "after the account's balance date, and every later sync rolls new "
            "ones forward; `account set-balance` remains the true-up for "
            "interest. Errors return {ok: false, error} — fix and retry. "
            "Confirm the account, source, and pattern with the user before "
            "creating (list_transfer_links suggestions carry ready-made "
            "parameters)."
        )
    )
    def link_account_transfers(
        account_id: Annotated[
            str, Field(description="Manual account id (MANUAL-...) whose balance rolls forward.")
        ],
        source_account_id: Annotated[
            str, Field(description="Synced account id whose transactions fund it.")
        ],
        pattern: Annotated[
            str, Field(description="Pattern matched against payee and description.")
        ],
        match_type: Annotated[
            str, Field(description="'contains' (default) or 'regex'.")
        ] = "contains",
    ) -> dict[str, Any]:
        return _link_account_transfers(
            store,
            account_id=account_id,
            source_account_id=source_account_id,
            pattern=pattern,
            match_type=match_type,
        )

    @mcp.tool(
        description=(
            "Delete a transfer link by id (get ids from list_transfer_links). "
            "Already-applied transfers stay in the balance and can never "
            "double-count on re-link. Unknown ids return {ok: false, error}. "
            "Confirm with the user before deleting — links are user-authored "
            "configuration."
        )
    )
    def unlink_account_transfers(
        link_id: Annotated[int, Field(ge=1, description="Link id from list_transfer_links.")],
    ) -> dict[str, Any]:
        return _unlink_account_transfers(store, link_id)

    @mcp.tool(
        description=(
            "Update a MANUAL account's balance — a true-up for interest, "
            "market movement, or anything transfer sums can't see. MANUAL "
            "accounts only (ids starting MANUAL-...): synced accounts get "
            "their balance from SimpleFIN and a manual edit would be "
            "overwritten. Writes the new balance plus a balance_snapshots "
            "row so net-worth-over-time reflects it, and re-anchors the "
            "account's transfer links at as_of: linked transfers posted "
            "after the true-up still auto-apply — each once and only once — "
            "so nothing double-counts. Example: account='Apple Savings' (or "
            "'MANUAL-1a2b...'), balance=30450.12, as_of='2026-07-01' "
            "(optional ISO date/datetime, default now UTC, never future). "
            "Errors return {ok: false, error} with a did-you-mean for "
            "account-name typos. Confirm the account and amount with the "
            "user before writing. AUTOMATIC balance roll-forward on "
            "matching transfers is the transfer-links feature "
            "(link_account_transfers / list_transfer_links) — use this tool "
            "for drift/interest true-ups, not per-contribution bookkeeping."
        )
    )
    def set_account_balance(
        account: Annotated[
            str,
            Field(
                description=(
                    "Account id (e.g. 'MANUAL-1a2b...') or account name "
                    "(case-insensitive, e.g. 'Apple Savings')."
                )
            ),
        ],
        balance: Annotated[
            float,
            Field(
                description=(
                    "The account's true current balance, e.g. 30450.12. "
                    "Signed as the account carries it."
                )
            ),
        ],
        as_of: Annotated[
            str | None,
            Field(
                description=(
                    "Optional ISO date or datetime the balance was observed "
                    "(e.g. '2026-07-01' or '2026-07-01T12:00:00Z'). Default "
                    "now (UTC). Must not be in the future."
                )
            ),
        ] = None,
    ) -> dict[str, Any]:
        # Same wire-boundary rationale as set_goal: str() gives the exact
        # shortest repr, so JSON amounts convert to Decimal without float
        # artifacts before reaching the shared write path.
        return _set_account_balance(
            store, account=account, balance=Decimal(str(balance)), as_of=as_of
        )

    return mcp
