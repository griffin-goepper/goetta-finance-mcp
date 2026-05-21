from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from goetta_finance.models import Account, AccountType, BalanceSnapshot, Transaction
from goetta_finance.store.duckdb_store import DuckDBStore
from goetta_finance.web.aggregations import (
    monthly_income_spending,
    net_worth_series,
    recent_sync_runs,
)


def _seed_accounts(store: DuckDBStore) -> None:
    store.upsert_accounts(
        [
            Account(
                id="a1",
                org_name="Chase",
                name="Checking",
                balance=Decimal("100.00"),
                balance_date=datetime(2026, 5, 1, tzinfo=UTC),
                type=AccountType.CHECKING,
            ),
            Account(
                id="a2",
                org_name="Vanguard",
                name="Brokerage",
                balance=Decimal("50000.00"),
                balance_date=datetime(2026, 5, 1, tzinfo=UTC),
                type=AccountType.INVESTMENT,
            ),
        ]
    )


def test_monthly_income_spending_groups_by_month(store: DuckDBStore) -> None:
    _seed_accounts(store)
    store.upsert_transactions(
        [
            Transaction(
                id="t1",
                account_id="a1",
                posted=datetime(2026, 4, 10, tzinfo=UTC),
                amount=Decimal("-100.00"),
                description="rent april",
            ),
            Transaction(
                id="t2",
                account_id="a1",
                posted=datetime(2026, 4, 20, tzinfo=UTC),
                amount=Decimal("5000.00"),
                description="paycheck april",
            ),
            Transaction(
                id="t3",
                account_id="a1",
                posted=datetime(2026, 5, 5, tzinfo=UTC),
                amount=Decimal("-250.00"),
                description="groceries may",
            ),
        ]
    )
    rows = monthly_income_spending(store, months=3, now=datetime(2026, 5, 16, tzinfo=UTC))
    assert len(rows) == 3  # March (zero), April, May
    by_month = {r.month: r for r in rows}
    assert by_month[date(2026, 3, 1)].income == Decimal("0")
    assert by_month[date(2026, 3, 1)].spending == Decimal("0")
    assert by_month[date(2026, 4, 1)].income == Decimal("5000.00")
    assert by_month[date(2026, 4, 1)].spending == Decimal("100.00")
    assert by_month[date(2026, 5, 1)].income == Decimal("0")
    assert by_month[date(2026, 5, 1)].spending == Decimal("250.00")


def test_monthly_income_spending_handles_year_boundary(store: DuckDBStore) -> None:
    _seed_accounts(store)
    rows = monthly_income_spending(store, months=3, now=datetime(2026, 1, 15, tzinfo=UTC))
    months = [r.month for r in rows]
    assert months == [date(2025, 11, 1), date(2025, 12, 1), date(2026, 1, 1)]


def test_net_worth_series_sums_latest_per_account(store: DuckDBStore) -> None:
    _seed_accounts(store)
    snaps = [
        # a1: 100 on day 1, 150 on day 3
        BalanceSnapshot(
            account_id="a1",
            timestamp=datetime(2026, 5, 1, tzinfo=UTC),
            balance=Decimal("100.00"),
        ),
        BalanceSnapshot(
            account_id="a1",
            timestamp=datetime(2026, 5, 3, tzinfo=UTC),
            balance=Decimal("150.00"),
        ),
        # a2: 1000 on day 2 only
        BalanceSnapshot(
            account_id="a2",
            timestamp=datetime(2026, 5, 2, tzinfo=UTC),
            balance=Decimal("1000.00"),
        ),
    ]
    for s in snaps:
        store.record_balance_snapshot(s)

    series = net_worth_series(store, days=30, now=datetime(2026, 5, 16, tzinfo=UTC))
    by_day = {p.day: p.balance for p in series}
    # Day 1: a1=100. a2 hasn't appeared yet.
    assert by_day[date(2026, 5, 1)] == Decimal("100.00")
    # Day 2: a1=100 (latest still), a2=1000 → 1100
    assert by_day[date(2026, 5, 2)] == Decimal("1100.00")
    # Day 3: a1=150 (new), a2=1000 (carried) → 1150
    assert by_day[date(2026, 5, 3)] == Decimal("1150.00")


def test_net_worth_series_respects_window(store: DuckDBStore) -> None:
    _seed_accounts(store)
    now = datetime(2026, 5, 16, tzinfo=UTC)
    old = BalanceSnapshot(
        account_id="a1",
        timestamp=now - timedelta(days=200),
        balance=Decimal("999.00"),
    )
    recent = BalanceSnapshot(
        account_id="a1",
        timestamp=now - timedelta(days=10),
        balance=Decimal("42.00"),
    )
    store.record_balance_snapshot(old)
    store.record_balance_snapshot(recent)

    series = net_worth_series(store, days=30, now=now)
    days = {p.day for p in series}
    assert old.timestamp.date() not in days
    assert recent.timestamp.date() in days


def test_net_worth_series_liability_negative_balance(store: DuckDBStore) -> None:
    """Liability with NEGATIVE balance (SimpleFIN credit-card convention) contributes
    exactly ``balance`` to net worth — sign already correct.

    Pins the outcome of the signed-balance formula end-to-end through
    net_worth_series. A refactor that changes how the formula is expressed
    but still satisfies the property passes; a refactor that drops the
    CASE WHEN fails because the math is wrong.
    """
    store.upsert_accounts(
        [
            Account(
                id="asset-1",
                org_name="Chase",
                name="Checking",
                balance=Decimal("1000.00"),
                balance_date=datetime(2026, 5, 1, tzinfo=UTC),
                type=AccountType.CHECKING,
            ),
            Account(
                id="cc-1",
                org_name="Amex",
                name="Gold",
                balance=Decimal("-500.00"),
                balance_date=datetime(2026, 5, 1, tzinfo=UTC),
                type=AccountType.CREDIT,
                is_liability=True,
            ),
        ]
    )
    store.record_balance_snapshot(
        BalanceSnapshot(
            account_id="asset-1",
            timestamp=datetime(2026, 5, 1, tzinfo=UTC),
            balance=Decimal("1000.00"),
        )
    )
    store.record_balance_snapshot(
        BalanceSnapshot(
            account_id="cc-1",
            timestamp=datetime(2026, 5, 1, tzinfo=UTC),
            balance=Decimal("-500.00"),
        )
    )
    series = net_worth_series(store, days=30, now=datetime(2026, 5, 16, tzinfo=UTC))
    by_day = {p.day: p.balance for p in series}
    # 1000 (asset) + -500 (liability, balance already negative) = 500
    assert by_day[date(2026, 5, 1)] == Decimal("500.00"), (
        f"expected 500 (1000 asset + -500 negative-balance liability), got {by_day[date(2026, 5, 1)]}"
    )


def test_net_worth_series_liability_positive_balance(store: DuckDBStore) -> None:
    """Liability with POSITIVE balance (loan-servicer convention) gets sign-flipped
    to contribute -ABS(balance) to net worth.

    Pins the outcome of the signed-balance formula end-to-end. This is the case
    that would silently inflate net worth by 2x the loan amount if the
    CASE WHEN were dropped — making this a load-bearing regression test.
    """
    store.upsert_accounts(
        [
            Account(
                id="asset-1",
                org_name="Chase",
                name="Checking",
                balance=Decimal("10000.00"),
                balance_date=datetime(2026, 5, 1, tzinfo=UTC),
                type=AccountType.CHECKING,
            ),
            Account(
                id="MANUAL-loan-1",
                org_name="Dept of Education",
                name="Federal Student Loans",
                balance=Decimal("22500.00"),
                balance_date=datetime(2026, 5, 1, tzinfo=UTC),
                type=AccountType.LOAN,
                is_manual=True,
                is_liability=True,
            ),
        ]
    )
    store.record_balance_snapshot(
        BalanceSnapshot(
            account_id="asset-1",
            timestamp=datetime(2026, 5, 1, tzinfo=UTC),
            balance=Decimal("10000.00"),
        )
    )
    store.record_balance_snapshot(
        BalanceSnapshot(
            account_id="MANUAL-loan-1",
            timestamp=datetime(2026, 5, 1, tzinfo=UTC),
            balance=Decimal("22500.00"),
        )
    )
    series = net_worth_series(store, days=30, now=datetime(2026, 5, 16, tzinfo=UTC))
    by_day = {p.day: p.balance for p in series}
    # 10000 (asset) + -22500 (liability with positive stored balance, sign flipped) = -12500
    assert by_day[date(2026, 5, 1)] == Decimal("-12500.00"), (
        f"expected -12500 (10000 asset minus 22500 positive-balance liability), "
        f"got {by_day[date(2026, 5, 1)]} — likely missing the -ABS(balance) flip in net_worth_series SQL"
    )


def test_recent_sync_runs_orders_newest_first(store: DuckDBStore) -> None:
    from goetta_finance.models import SyncRun

    base = datetime(2026, 5, 16, tzinfo=UTC)
    for i in range(3):
        store.record_sync_run(
            SyncRun(
                started_at=base + timedelta(minutes=i),
                finished_at=base + timedelta(minutes=i, seconds=30),
                accounts_touched=i + 1,
                transactions_new=i,
            )
        )
    rows = recent_sync_runs(store, limit=2)
    assert len(rows) == 2
    assert rows[0]["accounts_touched"] == 3  # newest
    assert rows[1]["accounts_touched"] == 2
