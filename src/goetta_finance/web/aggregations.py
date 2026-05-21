"""Pure data functions feeding the dashboard's charts and tables.

These exist so the chart builders stay thin and so the aggregations are
independently testable. All money values are returned as ``Decimal``;
chart builders downcast to ``float`` for Plotly.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import NamedTuple

from goetta_finance.store import FinanceStore


class MonthlyCashflow(NamedTuple):
    month: date  # first-of-month, UTC-derived
    income: Decimal
    spending: Decimal  # absolute value of negative amounts


class NetWorthPoint(NamedTuple):
    day: date
    balance: Decimal


def monthly_income_spending(
    store: FinanceStore, *, months: int = 12, now: datetime | None = None
) -> list[MonthlyCashflow]:
    """Sum income (positive amounts) and spending (abs of negative amounts)
    grouped by month for the last ``months`` calendar months. Pending
    transactions are excluded by upstream parsing (collector drops them),
    but we double-guard with ``pending = false`` anyway.

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
            date_trunc('month', posted) AS month,
            SUM(CASE WHEN amount > 0 THEN amount ELSE 0 END) AS income,
            SUM(CASE WHEN amount < 0 THEN -amount ELSE 0 END) AS spending
        FROM transactions
        WHERE posted >= ?
          AND pending = false
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


def net_worth_series(
    store: FinanceStore, *, days: int = 90, now: datetime | None = None
) -> list[NetWorthPoint]:
    """Aggregate per-account balance snapshots into daily total net worth.

    For each day in the window, find the latest snapshot for each account
    at or before that day and sum the **signed** balances. Days with no
    snapshot for any account are skipped (the chart's x axis is the union
    of days that had at least one snapshot).

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
        SELECT bs.account_id,
               bs.timestamp,
               CASE WHEN a.is_liability
                    THEN -ABS(bs.balance)
                    ELSE bs.balance
               END AS signed_balance
        FROM balance_snapshots bs
        JOIN accounts a ON a.id = bs.account_id
        WHERE bs.timestamp >= ?
        ORDER BY bs.account_id, bs.timestamp
        """,
        [since],
    )

    latest_per_account: dict[str, Decimal] = {}
    days_seen: set[date] = set()
    rows_by_day: dict[date, list[tuple[str, Decimal]]] = {}
    for row in rows:
        ts = row["timestamp"]
        day = ts.date() if isinstance(ts, datetime) else ts
        days_seen.add(day)
        rows_by_day.setdefault(day, []).append(
            (row["account_id"], _as_decimal(row["signed_balance"]))
        )

    out: list[NetWorthPoint] = []
    for day in sorted(days_seen):
        for acct, bal in rows_by_day[day]:
            latest_per_account[acct] = bal
        total = sum(latest_per_account.values(), Decimal("0"))
        out.append(NetWorthPoint(day=day, balance=total))
    return out


def recent_sync_runs(store: FinanceStore, *, limit: int = 10) -> list[dict[str, object]]:
    """Most recent sync_runs rows, newest first. Warnings/errors come back
    as JSON strings from DuckDB; callers can parse them as needed."""
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
    "MonthlyCashflow",
    "NetWorthPoint",
    "monthly_income_spending",
    "net_worth_series",
    "recent_sync_runs",
)
