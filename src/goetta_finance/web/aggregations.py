"""Pure data functions feeding the dashboard's charts and tables.

These exist so the chart builders stay thin and so the aggregations are
independently testable. All money values are returned as ``Decimal``;
chart builders downcast to ``float`` for Plotly.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import NamedTuple

from goetta_finance.store import FinanceStore
from goetta_finance.tools.spending_by_category import query_spending_by_category


class MonthlyCashflow(NamedTuple):
    month: date  # first-of-month, UTC-derived
    income: Decimal
    spending: Decimal  # absolute value of negative amounts


class NetWorthPoint(NamedTuple):
    day: date
    balance: Decimal


class CategoryTotal(NamedTuple):
    category: str
    total: Decimal
    transaction_count: int


class MonthlyCategorySpend(NamedTuple):
    month: date  # first-of-month, UTC-derived
    category: str
    total: Decimal
    transaction_count: int


def monthly_income_spending(
    store: FinanceStore, *, months: int = 12, now: datetime | None = None
) -> list[MonthlyCashflow]:
    """Category-aware monthly income / net-spending, last ``months`` months.

    Routes through ``transactions_with_category`` JOIN ``categories`` so
    the bars respect categorization (the bare-``transactions`` version
    predated it and double-counted transfers, miscounted refunds, and
    ignored hidden accounts):

    - **Spending** uses the SAME net-spending expression as the pie
      (``query_spending_by_category``): ``-amount`` over ``is_spending``
      categories (refunds net-reduce), EXCEPT a positive amount in
      ``Uncategorized`` which contributes 0. Non-spending categories
      (Transfers, Income) and hidden accounts contribute 0.
    - **Income** is strict: positive amounts whose resolved category is
      ``Income``. A raw positive amount is NOT income until categorized
      (it could be a refund or a transfer leg). So the income bar reads
      ~0 until the user adds an Income rule.

    Months with no activity are returned as zero-rows so the chart's bars
    line up on the x axis.
    """
    end = (now or datetime.now(tz=UTC)).astimezone(UTC)
    # Start of the (months-1)th-prior month, so we get exactly ``months`` buckets.
    start_year = end.year
    start_month = end.month - (months - 1)
    while start_month <= 0:
        start_month += 12
        start_year -= 1
    start = datetime(start_year, start_month, 1, tzinfo=UTC)

    rows = store.query_sql(
        """
        SELECT
            date_trunc('month', t.posted) AS month,
            SUM(CASE WHEN c.name = 'Income' AND t.amount > 0 THEN t.amount ELSE 0 END) AS income,
            SUM(CASE
                WHEN COALESCE(c.is_spending, TRUE) = FALSE THEN 0
                WHEN t.amount > 0 AND t.category = 'Uncategorized' THEN 0
                ELSE -t.amount
            END) AS spending
        FROM transactions_with_category t
        LEFT JOIN categories c ON c.name = t.category
        WHERE t.posted >= ?
          AND t.pending = false
          AND COALESCE(t.account_is_hidden, FALSE) = FALSE
        GROUP BY 1
        ORDER BY 1
        """,
        [start],
    )

    by_month: dict[date, MonthlyCashflow] = {}
    for row in rows:
        m = row["month"]
        bucket = m.date() if isinstance(m, datetime) else m
        by_month[bucket] = MonthlyCashflow(
            month=bucket,
            income=_as_decimal(row["income"]),
            spending=_as_decimal(row["spending"]),
        )

    out: list[MonthlyCashflow] = []
    for offset in range(months):
        year = start.year
        month = start.month + offset
        while month > 12:
            month -= 12
            year += 1
        bucket = date(year, month, 1)
        out.append(
            by_month.get(
                bucket, MonthlyCashflow(month=bucket, income=Decimal("0"), spending=Decimal("0"))
            )
        )
    return out


def monthly_spending_by_category(
    store: FinanceStore,
    *,
    months: int = 12,
    category: str | None = None,
    now: datetime | None = None,
) -> list[MonthlyCategorySpend]:
    """Net spending per (month, category), last ``months`` UTC calendar months.

    Long format, sparse: only (month, category) pairs with activity are
    returned — callers that chart a single category zero-fill their own
    buckets (the month window is derivable from ``months``/``now``).

    Semantics deliberately match the spending-cap goal math, NOT the
    ``monthly_income_spending`` bars:

    - Same net-spending CASE as ``query_spending_by_category`` (refunds
      reduce; a positive amount in ``Uncategorized`` contributes 0).
    - Hidden accounts excluded; non-spending categories excluded (an
      explicit ``category`` filter for a non-spending category returns
      no rows — the function answers "what did I spend").
    - **Pending transactions COUNT** — a spending cap is an early-warning
      device and pending charges are committed money (``goals.py``). So a
      current-month bucket here agrees with the goal card and the pie to
      the cent, but can exceed the pending-excluded bars while charges
      are settling.
    """
    end = (now or datetime.now(tz=UTC)).astimezone(UTC)
    # Start of the (months-1)th-prior month, so we get exactly ``months``
    # buckets — same window computation as ``monthly_income_spending``.
    start_year = end.year
    start_month = end.month - (months - 1)
    while start_month <= 0:
        start_month += 12
        start_year -= 1
    start = datetime(start_year, start_month, 1, tzinfo=UTC)

    where = (
        "t.posted >= ? AND COALESCE(t.account_is_hidden, FALSE) = FALSE"
        " AND COALESCE(c.is_spending, TRUE) = TRUE"
    )
    params: list[object] = [start]
    if category is not None:
        where += " AND t.category = ?"
        params.append(category)
    # ruff S608 / bandit B608: ``where`` is composed entirely of string
    # literals plus ``?`` placeholders bound via params. No user input is
    # interpolated (same audited pattern as query_spending_by_category).
    sql = f"""
        SELECT
            date_trunc('month', t.posted) AS month,
            t.category,
            SUM(CASE WHEN t.amount > 0 AND t.category = 'Uncategorized'
                     THEN 0 ELSE -t.amount END) AS total,
            COUNT(*) AS transaction_count
        FROM transactions_with_category t
        LEFT JOIN categories c ON c.name = t.category
        WHERE {where}
        GROUP BY 1, 2
        ORDER BY 1, total DESC
    """  # noqa: S608  # nosec B608
    rows = store.query_sql(sql, params)

    out: list[MonthlyCategorySpend] = []
    for row in rows:
        m = row["month"]
        bucket = m.date() if isinstance(m, datetime) else m
        out.append(
            MonthlyCategorySpend(
                month=bucket,
                category=str(row["category"]),
                total=_as_decimal(row["total"]),
                transaction_count=int(row["transaction_count"]),
            )
        )
    return out


def net_worth_series(
    store: FinanceStore, *, days: int = 90, now: datetime | None = None
) -> list[NetWorthPoint]:
    """Aggregate per-account balance snapshots into daily total net worth.

    For each day in the window, find the latest snapshot for each account
    at or before that day and sum the **signed** balances. The first point
    is the window boundary, followed by days on which a balance changed.

    Account history often starts at different times because connecting a
    new institution records its existing balance on that day. Treating the
    first observation as zero before that instant creates a fictitious net-
    worth gain (for example, connecting an established brokerage looks like
    a six-figure windfall). Seed every account at the window boundary with
    its latest older snapshot, or its earliest known snapshot when no older
    one exists. The latter is an estimate, but it preserves the truthful
    fact that "first observed" is not the same as "newly acquired."

    Signed-balance formula (matches the user-facing pattern documented in
    server.SQL_SCHEMA_HINT): a liability contributes ``-ABS(balance)``
    regardless of how the source signs it. This collapses SimpleFIN's
    negative-CC convention and the loan-servicer's positive-amount-owed
    convention to the same correct answer.
    """
    end = (now or datetime.now(tz=UTC)).astimezone(UTC)
    since = end - timedelta(days=days)
    rows = store.query_sql(
        """
        WITH signed_snapshots AS (
            SELECT bs.account_id,
                   bs.timestamp,
                   CASE WHEN a.is_liability
                        THEN -ABS(bs.balance)
                        ELSE bs.balance
                   END AS signed_balance
            FROM balance_snapshots bs
            JOIN accounts a ON a.id = bs.account_id
            WHERE bs.timestamp <= ?
              AND COALESCE(a.is_hidden, FALSE) = FALSE
        ), seed_timestamps AS (
            SELECT account_id, MAX(timestamp) AS timestamp
            FROM signed_snapshots
            WHERE timestamp < ?
            GROUP BY account_id
        )
        SELECT account_id, timestamp, signed_balance
        FROM signed_snapshots
        WHERE timestamp >= ?
        UNION ALL
        SELECT snapshots.account_id, snapshots.timestamp, snapshots.signed_balance
        FROM signed_snapshots snapshots
        JOIN seed_timestamps seeds
          ON seeds.account_id = snapshots.account_id
         AND seeds.timestamp = snapshots.timestamp
        ORDER BY account_id, timestamp
        """,
        [end, since, since],
    )

    history_by_account: dict[str, list[tuple[datetime, Decimal]]] = {}
    for row in rows:
        timestamp = row["timestamp"]
        if timestamp.tzinfo is None:
            timestamp = timestamp.replace(tzinfo=UTC)
        history_by_account.setdefault(str(row["account_id"]), []).append(
            (timestamp, _as_decimal(row["signed_balance"]))
        )

    latest_per_account: dict[str, Decimal] = {}
    rows_by_day: dict[date, list[tuple[str, Decimal]]] = {}
    for account_id, history in history_by_account.items():
        history.sort(key=lambda item: item[0])
        older = [item for item in history if item[0] < since]
        seed = older[-1] if older else history[0]
        latest_per_account[account_id] = seed[1]
        for timestamp, balance in history:
            if timestamp < since:
                continue
            rows_by_day.setdefault(timestamp.date(), []).append((account_id, balance))

    out: list[NetWorthPoint] = []
    days_seen = {since.date(), *rows_by_day.keys()} if latest_per_account else set()
    for day in sorted(days_seen):
        for acct, bal in rows_by_day.get(day, []):
            latest_per_account[acct] = bal
        total = sum(latest_per_account.values(), Decimal("0"))
        out.append(NetWorthPoint(day=day, balance=total))
    return out


def net_worth_coverage_start(store: FinanceStore) -> date | None:
    """First day on which every visible account has observed history.

    Points before this date use the earliest-known-balance baseline from
    :func:`net_worth_series` and should be labeled as estimates by clients.
    ``None`` means there are no visible accounts or at least one visible
    account has no balance snapshot yet.
    """
    rows = store.query_sql(
        """
        WITH first_snapshots AS (
            SELECT account_id, MIN(timestamp) AS first_snapshot
            FROM balance_snapshots
            GROUP BY account_id
        )
        SELECT CASE
                   WHEN COUNT(*) = 0 OR COUNT(first_snapshot) < COUNT(*) THEN NULL
                   ELSE MAX(first_snapshot)
               END AS complete_from
        FROM accounts
        LEFT JOIN first_snapshots ON first_snapshots.account_id = accounts.id
        WHERE COALESCE(accounts.is_hidden, FALSE) = FALSE
        """
    )
    if not rows or rows[0]["complete_from"] is None:
        return None
    complete_from = rows[0]["complete_from"]
    return complete_from.date() if isinstance(complete_from, datetime) else complete_from


def spending_by_category_last_n_days(
    store: FinanceStore, *, days: int = 30, now: datetime | None = None
) -> list[CategoryTotal]:
    """Spending per category over the last ``days`` calendar days.

    Calls the shared ``query_spending_by_category`` helper in
    ``tools/spending_by_category.py`` so the dashboard and the MCP
    tool always agree on the SQL contract. Income is excluded by
    default — matches the dashboard's intent ("what did I spend").

    Keeps Decimal precision through to the chart builder; the chart
    builder downcasts to float for Plotly (same pattern as
    ``net_worth_series`` / ``monthly_income_spending``).
    """
    end = (now or datetime.now(tz=UTC)).astimezone(UTC)
    start = end - timedelta(days=days)
    rows = query_spending_by_category(store, start, end, include_non_spending=False)
    return [
        CategoryTotal(
            category=str(row["category"]),
            total=_as_decimal(row["total"]),
            transaction_count=int(row["transaction_count"]),
        )
        for row in rows
    ]


def display_currency(store: FinanceStore) -> str:
    """Currency code for cross-account aggregate displays.

    If every visible (non-hidden) account shares one currency, use it —
    a UK-only user sees GBP on the net-worth chart, not USD. If accounts
    span currencies, return 'mixed' so the label is honest rather than
    silently summing apples and oranges under a dollar sign.
    Multi-currency *arithmetic* (FX conversion) is out of scope; this
    only fixes the label.
    """
    currencies = {a.currency for a in store.get_accounts()}
    if len(currencies) == 1:
        return next(iter(currencies))
    if not currencies:
        return "USD"
    return "mixed"


def parse_json_list(value: object) -> list[str]:
    """Parse a sync_runs ``warnings``/``errors`` cell into a list of strings.

    DuckDB hands the column back as a JSON string (or None); tolerate raw
    strings and pre-parsed lists too. Shared by the dashboard sync page
    and the JSON API (moved here from ``views.py`` when the second caller
    arrived — it belongs next to ``recent_sync_runs``, which produces the
    values it parses)."""
    if value in (None, "", "null"):
        return []
    if isinstance(value, list):
        return [str(v) for v in value]
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return [value]
        if isinstance(parsed, list):
            return [str(v) for v in parsed]
        return [str(parsed)]
    return [str(value)]


def recent_sync_runs(store: FinanceStore, *, limit: int = 10) -> list[dict[str, object]]:
    """Most recent sync_runs rows, newest first. Warnings/errors come back
    as JSON strings from DuckDB; parse with ``parse_json_list``."""
    rows = store.query_sql(
        """
        SELECT id, started_at, finished_at, accounts_touched,
               transactions_new, transactions_updated, warnings, errors
        FROM sync_runs
        ORDER BY id DESC
        LIMIT ?
        """,
        [int(limit)],
    )
    return rows


def _as_decimal(value: object) -> Decimal:
    if isinstance(value, Decimal):
        return value
    if value is None:
        return Decimal("0")
    return Decimal(str(value))


__all__: Sequence[str] = (
    "CategoryTotal",
    "MonthlyCashflow",
    "MonthlyCategorySpend",
    "NetWorthPoint",
    "display_currency",
    "monthly_income_spending",
    "monthly_spending_by_category",
    "net_worth_coverage_start",
    "net_worth_series",
    "parse_json_list",
    "recent_sync_runs",
    "spending_by_category_last_n_days",
)
