from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from goetta_finance.models import Account, AccountType, BalanceSnapshot, Transaction
from goetta_finance.store.duckdb_store import DuckDBStore
from goetta_finance.web.aggregations import (
    display_currency,
    monthly_income_spending,
    net_worth_series,
    recent_sync_runs,
    spending_by_category_last_n_days,
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
    # Income is strict (Income-categorized only) — the paycheck must be
    # overridden to Income to count on the income bar. A raw positive
    # amount is no longer income (it could be a refund/transfer leg).
    store.set_transaction_override("t2", "Income")
    rows = monthly_income_spending(store, months=3, now=datetime(2026, 5, 16, tzinfo=UTC))
    assert len(rows) == 3  # March (zero), April, May
    by_month = {r.month: r for r in rows}
    assert by_month[date(2026, 3, 1)].income == Decimal("0")
    assert by_month[date(2026, 3, 1)].spending == Decimal("0")
    assert by_month[date(2026, 4, 1)].income == Decimal("5000.00")  # via Income category
    assert by_month[date(2026, 4, 1)].spending == Decimal("100.00")  # rent only; paycheck excluded
    assert by_month[date(2026, 5, 1)].income == Decimal("0")
    assert by_month[date(2026, 5, 1)].spending == Decimal("250.00")


def test_uncategorized_positive_amount_does_not_net_reduce_spending(
    store: DuckDBStore,
) -> None:
    """The uncategorized-positive guard: a +$5000 uncategorized deposit
    must NOT be treated as a phantom refund dragging the spending bar to
    -4800. It contributes 0; the bar shows only the -$200 spend. And it
    is NOT income (not Income-categorized)."""
    _seed_accounts(store)
    store.upsert_transactions(
        [
            Transaction(
                id="up-deposit",
                account_id="a1",
                posted=datetime(2026, 4, 10, tzinfo=UTC),
                amount=Decimal("5000.00"),  # uncategorized positive
                description="ZZZ MYSTERY DEPOSIT",
            ),
            Transaction(
                id="up-spend",
                account_id="a1",
                posted=datetime(2026, 4, 12, tzinfo=UTC),
                amount=Decimal("-200.00"),  # uncategorized spend
                description="ZZZ MYSTERY SPEND",
            ),
        ]
    )
    rows = monthly_income_spending(store, months=2, now=datetime(2026, 4, 30, tzinfo=UTC))
    april = next(r for r in rows if r.month == date(2026, 4, 1))
    assert april.spending == Decimal("200.00"), "uncategorized positive must not net-reduce"
    assert april.income == Decimal("0"), "uncategorized positive is not income"


def test_monthly_income_spending_excludes_transfers_and_hidden(
    store: DuckDBStore,
) -> None:
    """Transfers (is_spending=FALSE) and hidden-account transactions
    contribute to neither bar."""
    _seed_accounts(store)
    store.upsert_accounts(
        [
            Account(
                id="hidden-acc",
                org_name="Old",
                name="Closed",
                balance=Decimal("0.00"),
                balance_date=datetime(2026, 4, 1, tzinfo=UTC),
                type=AccountType.CHECKING,
            )
        ]
    )
    store.upsert_transactions(
        [
            Transaction(
                id="real-spend",
                account_id="a1",
                posted=datetime(2026, 4, 5, tzinfo=UTC),
                amount=Decimal("-50.00"),
                description="ZZZ REAL SPEND",
            ),
            Transaction(
                id="a-transfer",
                account_id="a1",
                posted=datetime(2026, 4, 6, tzinfo=UTC),
                amount=Decimal("-3000.00"),  # would inflate spending pre-fix
                description="ZZZ XFER OUT",
            ),
            Transaction(
                id="hidden-spend",
                account_id="hidden-acc",
                posted=datetime(2026, 4, 7, tzinfo=UTC),
                amount=Decimal("-999.00"),  # hidden account
                description="ZZZ HIDDEN SPEND",
            ),
        ]
    )
    store.set_transaction_override("a-transfer", "Transfers")
    store.set_account_hidden("hidden-acc", True)
    rows = monthly_income_spending(store, months=2, now=datetime(2026, 4, 30, tzinfo=UTC))
    april = next(r for r in rows if r.month == date(2026, 4, 1))
    assert april.spending == Decimal("50.00")  # only the real spend
    assert april.income == Decimal("0")


def test_pie_and_monthly_bar_agree_on_net_spending(store: DuckDBStore) -> None:
    """The unification property as a pinned contract: for one mixed month
    (spend + refund + transfer + hidden + uncategorized positive), the
    pie's category-summed total == the monthly bar's spending value, to
    the cent. Both compute net spending the same way."""
    _seed_accounts(store)
    store.upsert_accounts(
        [
            Account(
                id="hid2",
                org_name="Old",
                name="Closed",
                balance=Decimal("0.00"),
                balance_date=datetime(2026, 4, 1, tzinfo=UTC),
                type=AccountType.CHECKING,
            )
        ]
    )
    store.upsert_transactions(
        [
            # Categorized spend (Dining via legacy STARBUCKS rule) + a refund.
            Transaction(
                id="m-sbux",
                account_id="a1",
                posted=datetime(2026, 4, 5, tzinfo=UTC),
                amount=Decimal("-40.00"),
                description="STARBUCKS STORE",
            ),
            Transaction(
                id="m-sbux-refund",
                account_id="a1",
                posted=datetime(2026, 4, 6, tzinfo=UTC),
                amount=Decimal("10.00"),  # refund → net-reduces Dining
                description="STARBUCKS STORE REFUND",
            ),
            # Uncategorized: a spend and an ambiguous positive (contributes 0).
            Transaction(
                id="m-uncat-spend",
                account_id="a1",
                posted=datetime(2026, 4, 7, tzinfo=UTC),
                amount=Decimal("-25.00"),
                description="ZZZ MYSTERY",
            ),
            Transaction(
                id="m-uncat-pos",
                account_id="a1",
                posted=datetime(2026, 4, 8, tzinfo=UTC),
                amount=Decimal("500.00"),  # ambiguous → 0
                description="ZZZ MYSTERY CREDIT",
            ),
            # Transfer (excluded) and hidden-account spend (excluded).
            Transaction(
                id="m-xfer",
                account_id="a1",
                posted=datetime(2026, 4, 9, tzinfo=UTC),
                amount=Decimal("-3000.00"),
                description="ZZZ XFER",
            ),
            Transaction(
                id="m-hidden",
                account_id="hid2",
                posted=datetime(2026, 4, 10, tzinfo=UTC),
                amount=Decimal("-777.00"),
                description="ZZZ HIDDEN",
            ),
        ]
    )
    store.set_transaction_override("m-xfer", "Transfers")
    store.set_account_hidden("hid2", True)

    fixed_now = datetime(2026, 4, 30, tzinfo=UTC)
    bar = next(
        r
        for r in monthly_income_spending(store, months=1, now=fixed_now)
        if r.month == date(2026, 4, 1)
    )
    pie = spending_by_category_last_n_days(store, days=30, now=fixed_now)
    pie_total = sum((p.total for p in pie), Decimal("0"))

    # Dining net = 40 - 10 = 30; Uncategorized = 25 (positive ignored).
    # Transfer + hidden excluded. Expected = 55.00 on both surfaces.
    assert bar.spending == Decimal("55.00")
    assert pie_total == bar.spending


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


# --- spending_by_category_last_n_days --------------------------------------


def test_spending_by_category_last_n_days_excludes_outside_window(
    store: DuckDBStore,
) -> None:
    """Seed transactions inside and outside the 30-day window;
    aggregation includes only the ones inside."""
    store.upsert_accounts(
        [
            Account(
                id="cat-a1",
                org_name="Test",
                name="Checking",
                balance=Decimal("100.00"),
                balance_date=datetime(2026, 5, 1, tzinfo=UTC),
                type=AccountType.CHECKING,
            )
        ]
    )
    fixed_now = datetime(2026, 5, 31, tzinfo=UTC)
    store.upsert_transactions(
        [
            # Inside the 30-day window — counts.
            Transaction(
                id="cat-inside",
                account_id="cat-a1",
                posted=fixed_now - timedelta(days=10),
                amount=Decimal("-25.00"),
                description="STARBUCKS STORE #1",
            ),
            # Outside the window — excluded.
            Transaction(
                id="cat-outside",
                account_id="cat-a1",
                posted=fixed_now - timedelta(days=60),
                amount=Decimal("-99.00"),
                description="STARBUCKS STORE #1",
            ),
        ]
    )
    series = spending_by_category_last_n_days(store, days=30, now=fixed_now)
    by_cat = {p.category: p for p in series}
    assert "Dining" in by_cat
    assert by_cat["Dining"].total == Decimal("25.00")
    assert by_cat["Dining"].transaction_count == 1


def test_net_worth_series_excludes_hidden_accounts(store: DuckDBStore) -> None:
    """Seed two accounts with snapshots; hide one; net-worth series
    should only contain contributions from the visible account.

    Pins the JOIN filter in web/aggregations.py:net_worth_series — the
    Fidelity-duplicate use case directly motivates this."""
    store.upsert_accounts(
        [
            Account(
                id="nw-vis",
                org_name="Test",
                name="Visible",
                balance=Decimal("100.00"),
                balance_date=datetime(2026, 5, 1, tzinfo=UTC),
                type=AccountType.CHECKING,
            ),
            Account(
                id="nw-hid",
                org_name="Test",
                name="Hidden",
                balance=Decimal("999.00"),
                balance_date=datetime(2026, 5, 1, tzinfo=UTC),
                type=AccountType.CHECKING,
            ),
        ]
    )
    for acct_id, bal in [("nw-vis", "100"), ("nw-hid", "999")]:
        store.record_balance_snapshot(
            BalanceSnapshot(
                account_id=acct_id,
                timestamp=datetime(2026, 5, 1, tzinfo=UTC),
                balance=Decimal(bal),
            )
        )
    store.set_account_hidden("nw-hid", True)

    series = net_worth_series(store, days=30, now=datetime(2026, 5, 16, tzinfo=UTC))
    # Only the visible account contributes — total is 100, not 1099.
    assert series[0].balance == Decimal("100")


def test_spending_by_category_last_n_days_excludes_income(
    store: DuckDBStore,
) -> None:
    """Default behavior: Income is excluded (dashboard intent = spending only)."""
    store.upsert_accounts(
        [
            Account(
                id="inc-a1",
                org_name="Test",
                name="Checking",
                balance=Decimal("100.00"),
                balance_date=datetime(2026, 5, 1, tzinfo=UTC),
                type=AccountType.CHECKING,
            )
        ]
    )
    fixed_now = datetime(2026, 5, 31, tzinfo=UTC)
    store.upsert_transactions(
        [
            Transaction(
                id="paycheck",
                account_id="inc-a1",
                posted=fixed_now - timedelta(days=5),
                amount=Decimal("3000.00"),
                description="ACME PAYROLL",
            ),
        ]
    )
    store.set_transaction_override("paycheck", "Income")
    series = spending_by_category_last_n_days(store, days=30, now=fixed_now)
    assert "Income" not in {p.category for p in series}


# --- display_currency --------------------------------------------------------


def test_display_currency_single_currency(store: DuckDBStore) -> None:
    """A GBP-only user sees GBP on aggregate labels, not a hardcoded USD
    (stranger-test principle: don't bake US assumptions into the display)."""
    store.upsert_accounts(
        [
            Account(
                id="gbp-1",
                org_name="Monzo",
                name="Current",
                currency="GBP",
                balance=Decimal("100.00"),
                balance_date=datetime(2026, 5, 1, tzinfo=UTC),
                type=AccountType.CHECKING,
            )
        ]
    )
    assert display_currency(store) == "GBP"


def test_display_currency_mixed_is_honest(store: DuckDBStore) -> None:
    """Accounts spanning currencies → 'mixed', not a silent USD sum-label.
    Hidden accounts don't contribute (a hidden EUR account shouldn't flip
    a USD-only display to 'mixed')."""
    store.upsert_accounts(
        [
            Account(
                id="usd-1",
                org_name="Chase",
                name="Checking",
                currency="USD",
                balance=Decimal("100.00"),
                balance_date=datetime(2026, 5, 1, tzinfo=UTC),
                type=AccountType.CHECKING,
            ),
            Account(
                id="eur-1",
                org_name="N26",
                name="Konto",
                currency="EUR",
                balance=Decimal("50.00"),
                balance_date=datetime(2026, 5, 1, tzinfo=UTC),
                type=AccountType.CHECKING,
            ),
        ]
    )
    assert display_currency(store) == "mixed"
    store.set_account_hidden("eur-1", True)
    assert display_currency(store) == "USD"
