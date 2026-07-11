"""Domain math tests for goals.py — read-time progress, pace, breaches.

Every test pins ``now`` so period boundaries and elapsed percentages
are deterministic. NOW is 2026-05-13T12:00Z: May has 31 days, so
12.5/31 days elapsed = 40.3% — a convenient mid-month reference for
ahead-of-pace vs behind-pace assertions.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta, timezone
from decimal import Decimal

from goetta_finance.goals import (
    balance_goal_progress,
    evaluate_goals,
    goal_breach_warnings,
    period_bounds,
    spending_cap_progress,
)
from goetta_finance.models import (
    Account,
    AccountType,
    BalanceSnapshot,
    Goal,
    GoalDirection,
    GoalKind,
    GoalPeriod,
    GoalStatus,
    Transaction,
)
from goetta_finance.store.duckdb_store import DuckDBStore

NOW = datetime(2026, 5, 13, 12, tzinfo=UTC)
MAY_ELAPSED = Decimal("40.3")  # 12.5 of 31 days, quantized 0.1


# --- period_bounds ----------------------------------------------------------


def test_period_bounds_month_mid() -> None:
    start, end = period_bounds(GoalPeriod.MONTH, NOW)
    assert start == datetime(2026, 5, 1, tzinfo=UTC)
    assert end == datetime(2026, 6, 1, tzinfo=UTC)


def test_period_bounds_december_rolls_to_january() -> None:
    start, end = period_bounds(GoalPeriod.MONTH, datetime(2026, 12, 31, 23, tzinfo=UTC))
    assert start == datetime(2026, 12, 1, tzinfo=UTC)
    assert end == datetime(2027, 1, 1, tzinfo=UTC)


def test_period_bounds_year() -> None:
    start, end = period_bounds(GoalPeriod.YEAR, NOW)
    assert start == datetime(2026, 1, 1, tzinfo=UTC)
    assert end == datetime(2027, 1, 1, tzinfo=UTC)


def test_period_bounds_non_utc_now_normalized() -> None:
    """A local-tz ``now`` buckets by its UTC instant, not its wall clock.
    (Fixed-offset tz rather than ZoneInfo — the Windows Store Python in
    dev has no tzdata package and the offset is all that matters here.)"""
    # 2026-05-31 21:00 at UTC-5 is 2026-06-01 02:00 UTC → June bucket.
    local = datetime(2026, 5, 31, 21, tzinfo=timezone(timedelta(hours=-5)))
    start, _ = period_bounds(GoalPeriod.MONTH, local)
    assert start == datetime(2026, 6, 1, tzinfo=UTC)


# --- spending cap helpers ---------------------------------------------------


def _seed_account(store: DuckDBStore, *, account_id: str = "g-a1", hidden: bool = False) -> None:
    store.upsert_accounts(
        [
            Account(
                id=account_id,
                name="Goal Checking",
                balance=Decimal("1000.00"),
                balance_date=NOW,
                type=AccountType.CHECKING,
            )
        ]
    )
    if hidden:
        store.set_account_hidden(account_id, True)


def _add_txn(
    store: DuckDBStore,
    txn_id: str,
    amount: str,
    *,
    posted: datetime | None = None,
    account_id: str = "g-a1",
    pending: bool = False,
    category: str | None = "Dining",
) -> None:
    store.upsert_transactions(
        [
            Transaction(
                id=txn_id,
                account_id=account_id,
                posted=posted or datetime(2026, 5, 10, tzinfo=UTC),
                amount=Decimal(amount),
                description=f"goal test txn {txn_id}",
                pending=pending,
            )
        ]
    )
    if category is not None:
        store.set_transaction_override(txn_id, category)


def _cap(
    store: DuckDBStore,
    *,
    amount: str = "400",
    category: str = "Dining",
    period: str = "month",
    name: str | None = None,
) -> Goal:
    return store.add_goal(
        name or f"{category} cap",
        kind="spending_cap",
        amount=Decimal(amount),
        category_name=category,
        period=period,
    )


# --- spending cap progress --------------------------------------------------


def test_cap_under_pace_is_on_track(store: DuckDBStore) -> None:
    _seed_account(store)
    _add_txn(store, "t-under", "-100.00")
    progress = spending_cap_progress(store, _cap(store), now=NOW)
    assert progress.current == Decimal("100.00")
    assert progress.percent == Decimal("25.0")
    assert progress.period_elapsed_percent == MAY_ELAPSED
    assert progress.status is GoalStatus.ON_TRACK
    assert progress.period_start == datetime(2026, 5, 1, tzinfo=UTC)
    assert progress.period_end == datetime(2026, 6, 1, tzinfo=UTC)


def test_cap_ahead_of_pace_is_at_risk(store: DuckDBStore) -> None:
    _seed_account(store)
    _add_txn(store, "t-risk", "-250.00")
    progress = spending_cap_progress(store, _cap(store), now=NOW)
    assert progress.percent == Decimal("62.5")
    assert progress.percent > progress.period_elapsed_percent
    assert progress.status is GoalStatus.AT_RISK


def test_cap_over_and_exactly_at(store: DuckDBStore) -> None:
    _seed_account(store)
    goal = _cap(store)
    _add_txn(store, "t-over", "-412.50")
    progress = spending_cap_progress(store, goal, now=NOW)
    assert progress.status is GoalStatus.OVER
    assert progress.percent == Decimal("103.1")

    _add_txn(store, "t-refund-to-cap", "12.50")  # refund back down to exactly 400
    progress = spending_cap_progress(store, goal, now=NOW)
    assert progress.current == Decimal("400.00")
    assert progress.status is GoalStatus.OVER  # current >= amount


def test_cap_refunds_can_push_negative(store: DuckDBStore) -> None:
    """A refund-dominated month yields negative net spending → on_track,
    negative percent (displayed as-is; the bar clamps, the number
    doesn't)."""
    _seed_account(store)
    _add_txn(store, "t-refund", "50.00")
    progress = spending_cap_progress(store, _cap(store), now=NOW)
    assert progress.current == Decimal("-50.00")
    assert progress.percent == Decimal("-12.5")
    assert progress.status is GoalStatus.ON_TRACK


def test_cap_month_boundary_microseconds(store: DuckDBStore) -> None:
    """Last microsecond of May counts; first microsecond of June doesn't."""
    _seed_account(store)
    _add_txn(
        store,
        "t-last-us",
        "-10.00",
        posted=datetime(2026, 5, 31, 23, 59, 59, 999999, tzinfo=UTC),
    )
    _add_txn(store, "t-june", "-77.00", posted=datetime(2026, 6, 1, tzinfo=UTC))
    progress = spending_cap_progress(store, _cap(store), now=NOW)
    assert progress.current == Decimal("10.00")


def test_cap_counts_pending_transactions(store: DuckDBStore) -> None:
    """Pins the decision: pending charges count toward caps (matching
    the by-category pie — a cap is an early-warning device and pending
    charges are committed money)."""
    _seed_account(store)
    _add_txn(store, "t-pending", "-90.00", pending=True)
    progress = spending_cap_progress(store, _cap(store), now=NOW)
    assert progress.current == Decimal("90.00")


def test_cap_excludes_hidden_account_transactions(store: DuckDBStore) -> None:
    _seed_account(store)
    _seed_account(store, account_id="g-hidden", hidden=True)
    _add_txn(store, "t-visible", "-30.00")
    _add_txn(store, "t-hidden", "-500.00", account_id="g-hidden")
    progress = spending_cap_progress(store, _cap(store), now=NOW)
    assert progress.current == Decimal("30.00")


def test_cap_on_non_spending_category_still_computes(store: DuckDBStore) -> None:
    """A cap on a category flipped to non-spending keeps working — the
    shared helper runs with include_non_spending=True and the total
    expression is identical in both modes."""
    _seed_account(store)
    _add_txn(store, "t-transfer", "-100.00", category="Transfers")
    progress = spending_cap_progress(store, _cap(store, category="Transfers"), now=NOW)
    assert progress.current == Decimal("100.00")


def test_cap_uncategorized_positive_contributes_zero(store: DuckDBStore) -> None:
    """Inherited guard: a positive amount in Uncategorized is ambiguous,
    not a phantom refund — contributes 0."""
    _seed_account(store)
    _add_txn(store, "t-mystery-credit", "500.00", category=None)
    _add_txn(store, "t-mystery-debit", "-20.00", category=None)
    progress = spending_cap_progress(store, _cap(store, category="Uncategorized"), now=NOW)
    assert progress.current == Decimal("20.00")


def test_cap_at_period_start_is_on_track(store: DuckDBStore) -> None:
    """elapsed == 0 never divides or flags at_risk; only OVER can fire."""
    _seed_account(store)
    _add_txn(store, "t-first", "-100.00", posted=datetime(2026, 5, 1, tzinfo=UTC))
    progress = spending_cap_progress(store, _cap(store), now=datetime(2026, 5, 1, tzinfo=UTC))
    assert progress.period_elapsed_percent == Decimal("0.0")
    assert progress.status is GoalStatus.ON_TRACK


def test_cap_year_period_spans_months(store: DuckDBStore) -> None:
    _seed_account(store)
    _add_txn(store, "t-feb", "-100.00", posted=datetime(2026, 2, 10, tzinfo=UTC))
    _add_txn(store, "t-may", "-150.00")
    progress = spending_cap_progress(store, _cap(store, period="year"), now=NOW)
    assert progress.current == Decimal("250.00")
    assert progress.period_start == datetime(2026, 1, 1, tzinfo=UTC)


def test_cap_zero_activity_category(store: DuckDBStore) -> None:
    """A category with no transactions this period reads 0, on_track."""
    _seed_account(store)
    progress = spending_cap_progress(store, _cap(store, category="Travel"), now=NOW)
    assert progress.current == Decimal("0")
    assert progress.status is GoalStatus.ON_TRACK


# --- balance goal helpers ---------------------------------------------------


def _balance_goal(
    *,
    amount: str = "10000",
    direction: GoalDirection = GoalDirection.AT_LEAST,
    target_date: date | None = None,
) -> Goal:
    return Goal(
        id=1,
        name="balance goal",
        kind=GoalKind.BALANCE,
        amount=Decimal(amount),
        account_id="b-a1",
        account_name="Balance Acct",
        direction=direction,
        target_date=target_date,
        created_at=NOW,
    )


def _acct(balance: str, *, liability: bool = False) -> Account:
    return Account(
        id="b-a1",
        name="Balance Acct",
        balance=Decimal(balance),
        balance_date=NOW,
        is_liability=liability,
    )


def _snaps(*points: tuple[int, str]) -> list[BalanceSnapshot]:
    """Build ascending snapshots from (days_ago, balance) pairs.
    Callers list oldest first (largest days_ago first)."""
    return [
        BalanceSnapshot(
            account_id="b-a1",
            timestamp=NOW - timedelta(days=days_ago),
            balance=Decimal(balance),
        )
        for days_ago, balance in points
    ]


# --- balance goal progress --------------------------------------------------


def test_balance_at_least_met() -> None:
    progress = balance_goal_progress(_balance_goal(), _acct("12000"), [], now=NOW)
    assert progress.status is GoalStatus.MET
    assert progress.current == Decimal("12000")
    assert progress.percent == Decimal("120.0")


def test_balance_at_least_unmet_no_history_is_on_track() -> None:
    """Insufficient data is not risk."""
    progress = balance_goal_progress(_balance_goal(), _acct("6500"), [], now=NOW)
    assert progress.status is GoalStatus.ON_TRACK
    assert progress.monthly_delta is None
    assert progress.projected_date is None


def test_balance_trend_needs_two_weeks_of_span() -> None:
    snaps = _snaps((10, "6000"), (0, "6500"))  # only 10 days of history
    progress = balance_goal_progress(_balance_goal(), _acct("6500"), snaps, now=NOW)
    assert progress.monthly_delta is None
    assert progress.status is GoalStatus.ON_TRACK


def test_balance_at_least_on_track_with_target_date() -> None:
    """Growing 1500 over 90 days toward a far deadline: on_track, with
    monthly delta, projection, and required-per-month all populated."""
    snaps = _snaps((90, "5000"), (0, "6500"))
    goal = _balance_goal(target_date=date(2027, 6, 1))
    progress = balance_goal_progress(goal, _acct("6500"), snaps, now=NOW)
    assert progress.status is GoalStatus.ON_TRACK
    assert progress.monthly_delta == Decimal("507.33")  # 1500/90 * 30.44
    assert progress.required_monthly == Decimal("277.45")  # 3500 over 384 days
    assert progress.projected_date is not None
    # ~210 days out at 16.67/day for the 3500 gap.
    assert date(2026, 12, 5) <= progress.projected_date <= date(2026, 12, 12)


def test_balance_at_least_projection_past_deadline_is_at_risk() -> None:
    snaps = _snaps((90, "5000"), (0, "6500"))  # projects ~Dec 2026
    goal = _balance_goal(target_date=date(2026, 7, 1))
    progress = balance_goal_progress(goal, _acct("6500"), snaps, now=NOW)
    assert progress.status is GoalStatus.AT_RISK


def test_balance_at_least_backwards_trend_with_deadline_is_at_risk() -> None:
    snaps = _snaps((90, "7000"), (0, "6500"))  # shrinking
    goal = _balance_goal(target_date=date(2027, 6, 1))
    progress = balance_goal_progress(goal, _acct("6500"), snaps, now=NOW)
    assert progress.status is GoalStatus.AT_RISK
    assert progress.projected_date is None
    assert progress.monthly_delta is not None
    assert progress.monthly_delta < 0


def test_balance_at_least_backwards_trend_without_deadline_stays_on_track() -> None:
    """No deadline → no basis for at_risk; the negative monthly delta is
    still shown so the surfaces convey direction."""
    snaps = _snaps((90, "7000"), (0, "6500"))
    progress = balance_goal_progress(_balance_goal(), _acct("6500"), snaps, now=NOW)
    assert progress.status is GoalStatus.ON_TRACK
    assert progress.monthly_delta is not None
    assert progress.monthly_delta < 0


def test_balance_at_least_past_deadline_unmet_is_at_risk() -> None:
    """A goal whose target_date has passed while unmet goes at_risk.
    (Write-time validation refuses past dates at creation; this covers
    dates that pass afterwards.)"""
    goal = _balance_goal(target_date=date(2026, 5, 1))
    progress = balance_goal_progress(goal, _acct("6500"), [], now=NOW)
    assert progress.status is GoalStatus.AT_RISK
    assert progress.required_monthly is None


def test_balance_liability_at_most_negative_sign_convention() -> None:
    """SimpleFIN credit cards report negative balances; the liability
    abs rule reads -1800 as 'owes 1800' → under a 2000 ceiling = met."""
    goal = _balance_goal(amount="2000", direction=GoalDirection.AT_MOST)
    progress = balance_goal_progress(goal, _acct("-1800", liability=True), [], now=NOW)
    assert progress.current == Decimal("1800")
    assert progress.status is GoalStatus.MET
    assert progress.percent == Decimal("90.0")


def test_balance_liability_at_most_positive_sign_convention() -> None:
    """Loan servicers report positive amount-owed; same result."""
    goal = _balance_goal(amount="2000", direction=GoalDirection.AT_MOST)
    progress = balance_goal_progress(goal, _acct("1800", liability=True), [], now=NOW)
    assert progress.current == Decimal("1800")
    assert progress.status is GoalStatus.MET


def test_balance_at_most_breached_is_over_with_pace_fields() -> None:
    """Above the ceiling → OVER, but the paydown trend still projects
    when the target will be reached."""
    goal = _balance_goal(amount="2000", direction=GoalDirection.AT_MOST)
    snaps = _snaps((30, "-3100"), (0, "-2500"))  # paying down 20/day
    progress = balance_goal_progress(goal, _acct("-2500", liability=True), snaps, now=NOW)
    assert progress.status is GoalStatus.OVER
    assert progress.current == Decimal("2500")
    assert progress.monthly_delta == Decimal("608.80")  # 20/day toward goal
    assert progress.projected_date == (NOW + timedelta(days=25)).date()


def test_balance_at_most_non_liability_over() -> None:
    goal = _balance_goal(amount="2000", direction=GoalDirection.AT_MOST)
    progress = balance_goal_progress(goal, _acct("2500"), [], now=NOW)
    assert progress.status is GoalStatus.OVER


# --- evaluate_goals (store-backed) -------------------------------------------


def test_evaluate_goals_mixed_kinds_and_hidden_balance_account(
    store: DuckDBStore,
) -> None:
    """Balance goals on hidden accounts evaluate normally — the goal
    names the account explicitly; hiding only affects default reads."""
    _seed_account(store)
    _add_txn(store, "t-mix", "-250.00")
    _cap(store, name="A dining cap")
    store.upsert_accounts(
        [
            Account(
                id="g-savings",
                name="Hidden Savings",
                balance=Decimal("12000.00"),
                balance_date=NOW,
                type=AccountType.SAVINGS,
            )
        ]
    )
    store.set_account_hidden("g-savings", True)
    store.add_goal(
        "B emergency fund",
        kind="balance",
        amount=Decimal("10000"),
        account_id="g-savings",
        direction="at_least",
    )
    progresses = evaluate_goals(store, now=NOW)
    assert [p.goal.name for p in progresses] == ["A dining cap", "B emergency fund"]
    cap_progress, balance_progress = progresses
    assert cap_progress.status is GoalStatus.AT_RISK
    assert balance_progress.status is GoalStatus.MET
    assert balance_progress.current == Decimal("12000.00")


def test_evaluate_goals_empty_store(store: DuckDBStore) -> None:
    assert evaluate_goals(store, now=NOW) == []


# --- goal_breach_warnings ----------------------------------------------------


def test_breach_warnings_fire_only_on_over(store: DuckDBStore) -> None:
    _seed_account(store)
    # Over cap → fires.
    _add_txn(store, "t-b1", "-450.00")
    _cap(store, name="Dining blown")
    # At-risk cap (ahead of pace, under cap) → silent.
    _add_txn(store, "t-b2", "-250.00", category="Groceries")
    _cap(store, category="Groceries", name="Groceries pacey")
    # Unmet at_least → silent (normal saving state).
    store.add_goal(
        "Savings journey",
        kind="balance",
        amount=Decimal("99999"),
        account_id="g-a1",
        direction="at_least",
    )
    # Breached at_most ceiling → fires.
    store.upsert_accounts(
        [
            Account(
                id="g-card",
                name="Credit Card",
                balance=Decimal("-2500.00"),
                balance_date=NOW,
                type=AccountType.CREDIT,
            )
        ]
    )
    store.set_account_liability("g-card", True)
    store.add_goal(
        "Card ceiling",
        kind="balance",
        amount=Decimal("2000"),
        account_id="g-card",
        direction="at_most",
    )

    lines = goal_breach_warnings(store, now=NOW)
    assert len(lines) == 2
    blown = next(line for line in lines if "Dining blown" in line)
    assert "450.00" in blown
    assert "400" in blown
    assert "Dining" in blown
    ceiling = next(line for line in lines if "Card ceiling" in line)
    assert "2500" in ceiling
    assert "ceiling 2000" in ceiling
    assert "Credit Card" in ceiling


def test_breach_warnings_never_contain_transaction_descriptions(
    store: DuckDBStore,
) -> None:
    """Logging rule: goal/category/account names and amounts only."""
    _seed_account(store)
    store.upsert_transactions(
        [
            Transaction(
                id="t-secret",
                account_id="g-a1",
                posted=datetime(2026, 5, 10, tzinfo=UTC),
                amount=Decimal("-450.00"),
                description="VENMO MEMO do not log this",
            )
        ]
    )
    store.set_transaction_override("t-secret", "Dining")
    _cap(store)
    lines = goal_breach_warnings(store, now=NOW)
    assert lines
    assert all("VENMO" not in line and "do not log" not in line for line in lines)
