"""Read-time goal evaluation: progress, pace, and breach warnings.

This is the ONE home for goal math. CLI ``goal list``, the MCP
``list_goals`` tool, the dashboard ``/goals`` page, and the post-sync
breach summary all call :func:`evaluate_goals` — never re-derive
spending or pace math per surface (consistent-metric rule). Historical
per-period actuals for a cap (:func:`spending_cap_history`) live here
for the same reason: the JSON API's goal-history endpoint must agree
with the goal card to the cent.

Spending-cap totals reuse ``query_spending_by_category`` (the pie's
helper) so caps, the by-category pie, and the monthly bars agree to the
cent: net spending (refunds reduce the total), hidden accounts
excluded, a positive amount in Uncategorized contributes 0, and pending
transactions COUNT (the pie includes them; a cap is an early-warning
device and pending charges are committed money).

Periods are UTC calendar buckets, matching the dashboard's
``date_trunc('month', posted)`` — a late-night local transaction can
land in the next bucket, accepted for exact consistency with the bars.

Balance goals evaluate ``abs(balance)`` for liability accounts: the
user's mental number for a credit card or loan is "how much do I owe",
so ``at_most 2000`` means "owe under 2000" whether the institution
signs the balance negative (SimpleFIN credit cards) or positive
(amount-owed loan servicers).

Contribution goals (migration 0014) count money INTO the goal's own
account per UTC calendar period: the ABSOLUTE value of settled feed
rows matching the goal's pattern (brokerages commonly sign cash-in
negative — sign is presentation, not direction), plus the
transfer-link applications ledger (already signed money-in), plus an
optional pre-history baseline counted into the period containing
``baseline_date``. Ahead of the clock is GOOD here — the status pace
comparison is the INVERSE of spending caps.
"""

from __future__ import annotations

import calendar
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import NamedTuple

from goetta_finance.errors import StoreError
from goetta_finance.models import (
    Account,
    BalanceSnapshot,
    Goal,
    GoalDirection,
    GoalKind,
    GoalPeriod,
    GoalProgress,
    GoalStatus,
)
from goetta_finance.store import FinanceStore
from goetta_finance.tools.spending_by_category import (
    query_category_spending_by_bucket,
    query_spending_by_category,
)
from goetta_finance.transfers import pending_transfer_delta

_DAYS_PER_MONTH = Decimal("30.44")  # mean Gregorian month
_TREND_LOOKBACK_DAYS = 90
_TREND_MIN_SNAPSHOTS = 2
_TREND_MIN_SPAN_DAYS = 14
# Cap trend extrapolation at ~100 years so a near-flat trend can't
# overflow timedelta or produce an absurd "projected 2450-01-01".
_PROJECTION_MAX_DAYS = 36500

_PERCENT_Q = Decimal("0.1")
_MONEY_Q = Decimal("0.01")


def period_bounds(period: GoalPeriod, now: datetime) -> tuple[datetime, datetime]:
    """UTC calendar bucket containing ``now``: (start, exclusive end)."""
    now = now.astimezone(UTC)
    if period is GoalPeriod.MONTH:
        start = datetime(now.year, now.month, 1, tzinfo=UTC)
        if now.month == 12:
            end = datetime(now.year + 1, 1, 1, tzinfo=UTC)
        else:
            end = datetime(now.year, now.month + 1, 1, tzinfo=UTC)
    else:
        start = datetime(now.year, 1, 1, tzinfo=UTC)
        end = datetime(now.year + 1, 1, 1, tzinfo=UTC)
    return start, end


def _as_decimal(value: object) -> Decimal:
    if isinstance(value, Decimal):
        return value
    if value is None:
        return Decimal("0")
    return Decimal(str(value))


def _aware_utc(dt: datetime) -> datetime:
    return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt.astimezone(UTC)


def _payday_dates(interval: str, anchor: date, lo: date, hi: date) -> list[date]:
    """All scheduled paydays p with ``lo <= p <= hi``, ascending.

    The series extends BOTH directions from the anchor: weekly/biweekly
    is ``anchor + k*7/14 days`` for ALL integer k (an anchor of
    2026-07-10 still yields the 2026-01-09..07-10 biweekly paydays);
    monthly is the anchor's day-of-month in every month, clamped to the
    month's last day (anchor Jan 31 pays Feb 28). Pure UTC date math —
    the callers translate period datetimes to dates at midnight-UTC
    boundaries.
    """
    if hi < lo:
        return []
    if interval in ("weekly", "biweekly"):
        step = 7 if interval == "weekly" else 14
        # Smallest k with anchor + k*step >= lo (k may be negative).
        k = -(-(lo - anchor).days // step)  # ceil division
        out = []
        payday = anchor + timedelta(days=k * step)
        while payday <= hi:
            out.append(payday)
            payday += timedelta(days=step)
        return out
    if interval != "monthly":  # pragma: no cover - validators + store gate
        raise ValueError(f"unknown recurring interval: {interval!r}")
    out = []
    year, month = lo.year, lo.month
    while (year, month) <= (hi.year, hi.month):
        last_day = calendar.monthrange(year, month)[1]
        payday = date(year, month, min(anchor.day, last_day))
        if lo <= payday <= hi:
            out.append(payday)
        month += 1
        if month == 13:
            month, year = 1, year + 1
    return out


def _required_monthly(gap: Decimal, target: date, now: datetime) -> Decimal | None:
    """Months-remaining pace: gap over (days to ``target`` / mean month).

    THE months-remaining helper — dated balance goals use it against
    ``target_date`` and year-period contribution goals against their
    period end, so "need X/mo" means the same thing on both. ``None``
    at or past the target date (pace toward the past is meaningless).
    """
    days_remaining = (target - now.date()).days
    if days_remaining <= 0:
        return None
    return (gap / (Decimal(days_remaining) / _DAYS_PER_MONTH)).quantize(_MONEY_Q)


def spending_cap_progress(
    store: FinanceStore,
    goal: Goal,
    *,
    now: datetime | None = None,
    totals_by_category: dict[str, Decimal] | None = None,
) -> GoalProgress:
    """Net spending in the goal's category this period vs the cap.

    ``include_non_spending=True`` so a cap on a category the user later
    flips to non-spending still returns a row — the total expression is
    identical in both modes; the flag only gates which rows appear.

    ``totals_by_category`` lets :func:`evaluate_goals` share ONE
    spending query across every cap on the same period instead of
    re-scanning per goal; it must be the {category: total} mapping of
    exactly the query below for this goal's period bounds. Omitted, the
    query runs here (the single-goal path).
    """
    if goal.period is None or goal.category_name is None:  # pragma: no cover
        raise StoreError(f"spending_cap goal {goal.id} is missing category or period")
    now = (now or datetime.now(tz=UTC)).astimezone(UTC)
    start, end = period_bounds(goal.period, now)
    if totals_by_category is None:
        # The shared helper's end bound is inclusive (posted <= end); back
        # off one microsecond so the first instant of the next bucket is
        # excluded.
        rows = query_spending_by_category(
            store, start, end - timedelta(microseconds=1), include_non_spending=True
        )
        totals_by_category = {str(r["category"]): _as_decimal(r["total"]) for r in rows}
    current = totals_by_category.get(goal.category_name, Decimal("0"))
    percent = (current / goal.amount * 100).quantize(_PERCENT_Q)
    elapsed = (
        Decimal(int((now - start).total_seconds()))
        / Decimal(int((end - start).total_seconds()))
        * 100
    ).quantize(_PERCENT_Q)
    if current >= goal.amount:
        status = GoalStatus.OVER
    elif elapsed > 0 and percent > elapsed:
        # Ahead of linear pace — the run-rate projects over the cap.
        # Noisy in the first days of a period (accepted; the surfaces
        # always show both percentages so the user can judge), and
        # breach warnings never fire on AT_RISK.
        status = GoalStatus.AT_RISK
    else:
        status = GoalStatus.ON_TRACK
    return GoalProgress(
        goal=goal,
        status=status,
        current=current,
        target=goal.amount,
        percent=percent,
        period_start=start,
        period_end=end,
        period_elapsed_percent=elapsed,
    )


class SpendingCapPeriod(NamedTuple):
    """One historical bucket of a spending-cap goal's actuals."""

    period_start: datetime
    period_end: datetime  # exclusive
    actual: Decimal
    over: bool  # actual >= cap — same comparison as GoalStatus.OVER


def spending_cap_history(
    store: FinanceStore, goal: Goal, *, periods: int = 12, now: datetime | None = None
) -> list[SpendingCapPeriod]:
    """Per-period net spending vs the cap, oldest first, newest last.

    Buckets are the goal's own period granularity (month or year), walked
    backwards from the bucket containing ``now``. All buckets come from
    ONE :func:`query_category_spending_by_bucket` call (same net-spending
    CASE as :func:`spending_cap_progress`, pending included, hidden
    excluded, microsecond end backoff, UTC ``date_trunc`` buckets ==
    :func:`period_bounds` buckets), so the newest bucket always equals
    the goal card's ``current`` to the cent — previously this looped one
    full spending query per bucket.
    """
    if goal.period is None or goal.category_name is None:  # pragma: no cover
        raise StoreError(f"spending_cap goal {goal.id} is missing category or period")
    now = (now or datetime.now(tz=UTC)).astimezone(UTC)
    bounds: list[tuple[datetime, datetime]] = []
    cursor = now
    for _ in range(periods):
        start, end = period_bounds(goal.period, cursor)
        bounds.append((start, end))
        cursor = start - timedelta(microseconds=1)
    oldest_start = bounds[-1][0]
    newest_end = bounds[0][1]
    actual_by_bucket = query_category_spending_by_bucket(
        store,
        oldest_start,
        newest_end - timedelta(microseconds=1),
        category=goal.category_name,
        bucket="month" if goal.period is GoalPeriod.MONTH else "year",
    )
    out: list[SpendingCapPeriod] = []
    for start, end in reversed(bounds):
        actual = actual_by_bucket.get(start.date(), Decimal("0"))
        out.append(
            SpendingCapPeriod(
                period_start=start,
                period_end=end,
                actual=actual,
                over=actual >= goal.amount,
            )
        )
    return out


def contribution_progress(
    store: FinanceStore,
    goal: Goal,
    *,
    now: datetime | None = None,
) -> GoalProgress:
    """Money contributed into the goal's account this period vs the target.

    ``current`` sums four sources over the UTC calendar bucket:

      1. The baseline, when ``baseline_date`` falls inside the period —
         contributions made before the feed's history starts.
      2. SUM(abs(amount)) of SETTLED feed rows matching the goal's
         pattern (``contribution_matched_sum`` — transfer-link match
         semantics against description OR payee). Absolute values on
         purpose: brokerages commonly sign cash-in negative.
      3. SUM(amount) of transfer-link application ledger rows posted in
         the period — already signed money-in, so a linked manual
         account needs no pattern at all.
      4. DECLARED recurring accrual (0015): paydays from the goal's
         schedule with period_start <= payday <= now, each accruing
         ``recurring_amount`` — by calculation, never observed in a
         feed (the payroll-deduction case no feed can see). The
         declared portion is carried on ``declared_total`` so the
         prose can disclose it.

    Pattern matches, ledger rows, and declared accrual draw from
    different sources (the account's own feed, the applications table,
    and pure schedule math), so a goal using several never
    double-counts.

    Status: MET at/over the target; otherwise ON_TRACK when the
    schedule alone covers the rest (current + future scheduled paydays
    this period >= target — prevents a perfectly-on-schedule payroll
    goal from flickering at_risk on biweekly step-lag) OR funding is at
    or ahead of the clock (percent >= elapsed); AT_RISK behind both —
    the INVERSE of caps, where ahead-of-pace is bad. Never OVER.

    ``required_monthly`` (year goals only, unmet): the remaining gap
    NET of future scheduled paydays, over the months left to the
    period end — the same months-remaining math dated balance goals
    use; ``None`` when the schedule alone covers it. ``pending_delta``
    previews matched still-pending feed rows plus pending linked
    transfers; ``None`` when the goal has no pattern AND the account
    has no links (nothing can ever be pending for it).
    """
    if goal.period is None or goal.account_id is None:  # pragma: no cover - table CHECK
        raise StoreError(f"contribution goal {goal.id} is missing account or period")
    now = (now or datetime.now(tz=UTC)).astimezone(UTC)
    start, end = period_bounds(goal.period, now)
    current = Decimal("0")
    if goal.baseline_amount is not None and goal.baseline_date is not None:
        baseline_at = _aware_utc(goal.baseline_date)
        if start <= baseline_at < end:
            current += goal.baseline_amount
    if goal.match_type is not None and goal.match_pattern is not None:
        current += store.contribution_matched_sum(
            goal.account_id,
            match_type=goal.match_type,
            pattern=goal.match_pattern,
            start=start,
            end=end,
        )
    current += store.transfer_applications_sum(goal.account_id, start=start, end=end)
    declared_total: Decimal | None = None
    scheduled_future = Decimal("0")
    if (
        goal.recurring_amount is not None
        and goal.recurring_interval is not None
        and goal.recurring_anchor is not None
    ):
        # Paydays with period_start <= p <= now accrue now (p < period_end
        # is implied: now is inside the period, so now.date() < end.date()).
        past_paydays = _payday_dates(
            goal.recurring_interval, goal.recurring_anchor, start.date(), now.date()
        )
        declared_total = goal.recurring_amount * len(past_paydays)
        current += declared_total
        # Paydays still to come THIS period: now < p < period_end.
        future_paydays = _payday_dates(
            goal.recurring_interval,
            goal.recurring_anchor,
            now.date() + timedelta(days=1),
            end.date() - timedelta(days=1),
        )
        scheduled_future = goal.recurring_amount * len(future_paydays)
    percent = (current / goal.amount * 100).quantize(_PERCENT_Q)
    elapsed = (
        Decimal(int((now - start).total_seconds()))
        / Decimal(int((end - start).total_seconds()))
        * 100
    ).quantize(_PERCENT_Q)
    if current >= goal.amount:
        status = GoalStatus.MET
    elif current + scheduled_future >= goal.amount or percent >= elapsed:
        status = GoalStatus.ON_TRACK
    else:
        status = GoalStatus.AT_RISK
    required_monthly: Decimal | None = None
    if status is not GoalStatus.MET and goal.period is GoalPeriod.YEAR:
        remaining = goal.amount - current - scheduled_future
        if remaining > 0:
            required_monthly = _required_monthly(remaining, end.date(), now)
    link_pending = pending_transfer_delta(store, goal.account_id)
    pending_delta: Decimal | None = None
    if goal.match_pattern is not None or link_pending is not None:
        pending_total = Decimal("0")
        if goal.match_type is not None and goal.match_pattern is not None:
            pending_total += store.contribution_matched_sum(
                goal.account_id,
                match_type=goal.match_type,
                pattern=goal.match_pattern,
                pending=True,
            )
        if link_pending is not None:
            pending_total += link_pending
        pending_delta = pending_total.quantize(_MONEY_Q)
    return GoalProgress(
        goal=goal,
        status=status,
        current=current,
        target=goal.amount,
        percent=percent,
        period_start=start,
        period_end=end,
        period_elapsed_percent=elapsed,
        required_monthly=required_monthly,
        pending_delta=pending_delta,
        declared_total=declared_total,
    )


class ContributionPeriod(NamedTuple):
    """One historical MONTH of a contribution goal's actuals."""

    period_start: datetime
    period_end: datetime  # exclusive
    actual: Decimal
    met: bool  # actual >= contribution_monthly_target(goal)


def contribution_monthly_target(goal: Goal) -> Decimal:
    """Per-month funding bar: the amount itself for month goals,
    amount/12 quantized to cents for year goals."""
    if goal.period is GoalPeriod.MONTH:
        return goal.amount
    return (goal.amount / 12).quantize(_MONEY_Q)


def contribution_history(
    store: FinanceStore, goal: Goal, *, months: int = 12, now: datetime | None = None
) -> list[ContributionPeriod]:
    """Per-MONTH contribution actuals, oldest first, newest last.

    Always monthly buckets regardless of the goal's own period — a year
    goal reads as twelve monthly funding checkpoints against
    :func:`contribution_monthly_target`. All months come from ONE
    bucketed query per source (matched feed rows via
    ``contribution_matched_monthly``, ledger rows via
    ``transfer_applications_monthly`` — identical expressions to
    :func:`contribution_progress`), with the baseline added to the
    bucket containing ``baseline_date`` and each ELAPSED scheduled
    payday's ``recurring_amount`` folded into its month's ``actual``
    (paydays after ``now`` don't tick yet — the bars step up every
    payday, same accrual rule as the goal card), so the newest bucket
    equals this month's contribution sum to the cent.
    """
    if goal.period is None or goal.account_id is None:  # pragma: no cover - table CHECK
        raise StoreError(f"contribution goal {goal.id} is missing account or period")
    now = (now or datetime.now(tz=UTC)).astimezone(UTC)
    bounds: list[tuple[datetime, datetime]] = []
    cursor = now
    for _ in range(months):
        start, end = period_bounds(GoalPeriod.MONTH, cursor)
        bounds.append((start, end))
        cursor = start - timedelta(microseconds=1)
    oldest_start = bounds[-1][0]
    newest_end = bounds[0][1]
    matched: dict[date, Decimal] = {}
    if goal.match_type is not None and goal.match_pattern is not None:
        matched = store.contribution_matched_monthly(
            goal.account_id,
            match_type=goal.match_type,
            pattern=goal.match_pattern,
            start=oldest_start,
            end=newest_end,
        )
    applied = store.transfer_applications_monthly(
        goal.account_id, start=oldest_start, end=newest_end
    )
    baseline_bucket: date | None = None
    if goal.baseline_amount is not None and goal.baseline_date is not None:
        baseline_at = _aware_utc(goal.baseline_date)
        baseline_bucket = date(baseline_at.year, baseline_at.month, 1)
    declared: dict[date, Decimal] = {}
    if (
        goal.recurring_amount is not None
        and goal.recurring_interval is not None
        and goal.recurring_anchor is not None
    ):
        # Elapsed paydays only (p <= now) — a payday later this month
        # hasn't accrued yet, matching the goal card.
        for payday in _payday_dates(
            goal.recurring_interval,
            goal.recurring_anchor,
            oldest_start.date(),
            min(now.date(), newest_end.date() - timedelta(days=1)),
        ):
            key = date(payday.year, payday.month, 1)
            declared[key] = declared.get(key, Decimal("0")) + goal.recurring_amount
    monthly_target = contribution_monthly_target(goal)
    out: list[ContributionPeriod] = []
    for start, end in reversed(bounds):
        key = start.date()
        actual = (
            matched.get(key, Decimal("0"))
            + applied.get(key, Decimal("0"))
            + declared.get(key, Decimal("0"))
        )
        if baseline_bucket == key and goal.baseline_amount is not None:
            actual += goal.baseline_amount
        out.append(
            ContributionPeriod(
                period_start=start,
                period_end=end,
                actual=actual,
                met=actual >= monthly_target,
            )
        )
    return out


def balance_goal_progress(
    goal: Goal,
    account: Account,
    snapshots: list[BalanceSnapshot],
    *,
    pending_raw: Decimal | None = None,
    now: datetime | None = None,
) -> GoalProgress:
    """Evaluate a balance goal. Pure — the caller fetches the account
    and its snapshot history (see :func:`evaluate_goals`).

    Status:
      - ``met``  — target satisfied (at_least: balance >= amount;
        at_most: balance <= amount).
      - ``over`` — an at_most goal whose balance exceeds the ceiling.
        Pace fields are still computed so the surfaces can show when
        the paydown trend reaches the target.
      - ``on_track`` / ``at_risk`` — unmet at_least goals: at_risk when
        the trend is flat/backwards or projects past ``target_date``;
        on_track otherwise (including when history is too thin to
        trend — insufficient data is not risk).

    ``monthly_delta`` is oriented toward the goal: positive means
    approaching the target regardless of direction. ``pending_raw``
    (the account's raw pending-transfer preview, from
    :func:`goetta_finance.transfers.pending_transfer_delta`) gets the
    same orientation on its way into ``pending_delta``.
    """
    now = (now or datetime.now(tz=UTC)).astimezone(UTC)
    current = abs(account.balance) if account.is_liability else account.balance
    percent = (current / goal.amount * 100).quantize(_PERCENT_Q)

    if goal.direction is GoalDirection.AT_LEAST:
        met = current >= goal.amount
        gap = goal.amount - current
    else:
        met = current <= goal.amount
        gap = current - goal.amount

    monthly_delta: Decimal | None = None
    toward_per_day: Decimal | None = None
    evaluated = [
        (s.timestamp, abs(s.balance) if account.is_liability else s.balance) for s in snapshots
    ]
    if len(evaluated) >= _TREND_MIN_SNAPSHOTS:
        first_ts, first_bal = evaluated[0]
        last_ts, last_bal = evaluated[-1]
        span_days = Decimal(int((last_ts - first_ts).total_seconds())) / Decimal(86400)
        if span_days >= _TREND_MIN_SPAN_DAYS:
            raw_per_day = (last_bal - first_bal) / span_days
            toward_per_day = (
                raw_per_day if goal.direction is GoalDirection.AT_LEAST else -raw_per_day
            )
            monthly_delta = (toward_per_day * _DAYS_PER_MONTH).quantize(_MONEY_Q)

    required_monthly: Decimal | None = None
    projected_date: date | None = None
    if not met:
        if goal.target_date is not None:
            required_monthly = _required_monthly(gap, goal.target_date, now)
        if toward_per_day is not None and toward_per_day > 0:
            days_needed = int(gap / toward_per_day)
            if days_needed <= _PROJECTION_MAX_DAYS:
                projected_date = now.date() + timedelta(days=days_needed)

    if met:
        status = GoalStatus.MET
    elif goal.direction is GoalDirection.AT_MOST:
        status = GoalStatus.OVER
    elif goal.target_date is not None:
        past_deadline = (goal.target_date - now.date()).days <= 0
        trend_backwards = toward_per_day is not None and toward_per_day <= 0
        projects_late = projected_date is not None and projected_date > goal.target_date
        misses_deadline = (
            toward_per_day is not None and toward_per_day > 0 and projected_date is None
        )
        if past_deadline or trend_backwards or projects_late or misses_deadline:
            status = GoalStatus.AT_RISK
        else:
            status = GoalStatus.ON_TRACK
    else:
        status = GoalStatus.ON_TRACK

    pending_delta: Decimal | None = None
    if pending_raw is not None:
        toward_pending = pending_raw if goal.direction is GoalDirection.AT_LEAST else -pending_raw
        pending_delta = toward_pending.quantize(_MONEY_Q)

    return GoalProgress(
        goal=goal,
        status=status,
        current=current,
        target=goal.amount,
        percent=percent,
        monthly_delta=monthly_delta,
        required_monthly=required_monthly,
        projected_date=projected_date,
        pending_delta=pending_delta,
    )


def evaluate_goals(store: FinanceStore, *, now: datetime | None = None) -> list[GoalProgress]:
    """Compute progress for every goal, ordered as ``list_goals`` returns.

    Balance goals on hidden accounts evaluate normally — the goal names
    the account explicitly; hiding only affects default read paths.
    (Spending caps never see hidden-account transactions — inherited
    from the shared spending helper.) A goal referencing a missing
    account raises ``StoreError`` — impossible while the FK and the
    delete_account guard hold, so fail loudly rather than skip.
    """
    now = (now or datetime.now(tz=UTC)).astimezone(UTC)
    goals = store.list_goals()
    accounts: dict[str, Account] = {}
    if any(g.kind is GoalKind.BALANCE for g in goals):
        accounts = {a.id: a for a in store.get_accounts(include_hidden=True)}
    # One spending query per DISTINCT cap period (month/year), not per
    # cap — with N caps on the same period this is the difference
    # between 1 scan and N identical ones (measured live: 6 caps made
    # /goals a 4.3s endpoint pre-0013).
    totals_by_period: dict[GoalPeriod, dict[str, Decimal]] = {}
    for period in {
        g.period for g in goals if g.kind is GoalKind.SPENDING_CAP and g.period is not None
    }:
        start, end = period_bounds(period, now)
        rows = query_spending_by_category(
            store, start, end - timedelta(microseconds=1), include_non_spending=True
        )
        totals_by_period[period] = {str(r["category"]): _as_decimal(r["total"]) for r in rows}
    out: list[GoalProgress] = []
    for goal in goals:
        if goal.kind is GoalKind.SPENDING_CAP:
            out.append(
                spending_cap_progress(
                    store,
                    goal,
                    now=now,
                    totals_by_category=totals_by_period.get(goal.period)
                    if goal.period is not None
                    else None,
                )
            )
        elif goal.kind is GoalKind.CONTRIBUTION:
            # Needs no Account row: progress reads the account's own
            # transactions and ledger, not its balance.
            out.append(contribution_progress(store, goal, now=now))
        else:
            if goal.account_id is None:  # pragma: no cover - table CHECK
                raise StoreError(f"balance goal {goal.id} has no account_id")
            account = accounts.get(goal.account_id)
            if account is None:
                raise StoreError(f"account not found: {goal.account_id}")
            snapshots = store.get_balance_history(
                goal.account_id, since=now - timedelta(days=_TREND_LOOKBACK_DAYS)
            )
            pending_raw = pending_transfer_delta(store, goal.account_id)
            out.append(
                balance_goal_progress(goal, account, snapshots, pending_raw=pending_raw, now=now)
            )
    return out


def goal_breach_warnings(store: FinanceStore, *, now: datetime | None = None) -> list[str]:
    """One-line breach messages for post-sync reporting.

    Fires only on status OVER: spending caps at/over the cap and
    at_most balance goals above the ceiling. Never AT_RISK (linear-pace
    noise) and never unmet at_least goals (that's the normal saving
    state). Messages carry goal/category/account names and amounts
    only — never transaction descriptions (logging rule: no transaction
    text at INFO+).
    """
    lines: list[str] = []
    for progress in evaluate_goals(store, now=now):
        if progress.status is not GoalStatus.OVER:
            continue
        goal = progress.goal
        if goal.kind is GoalKind.SPENDING_CAP:
            period_noun = "month" if goal.period is GoalPeriod.MONTH else "year"
            lines.append(
                f'goal "{goal.name}": over — {progress.current} of {progress.target} '
                f"({goal.category_name}) spent this {period_noun}"
            )
        else:
            account_label = goal.account_name or goal.account_id
            lines.append(
                f'goal "{goal.name}": over — {account_label} at {progress.current}, '
                f"ceiling {progress.target}"
            )
    return lines


def describe_goal(goal: Goal) -> str:
    """One-line human definition, shared by CLI and dashboard."""
    if goal.kind is GoalKind.SPENDING_CAP:
        period_noun = "month" if goal.period is GoalPeriod.MONTH else "year"
        return f"{goal.category_name} under {goal.amount} per {period_noun}"
    if goal.kind is GoalKind.CONTRIBUTION:
        period_noun = "month" if goal.period is GoalPeriod.MONTH else "year"
        label = goal.account_name or goal.account_id
        return f"contribute {goal.amount} to {label} per {period_noun}"
    direction = "at least" if goal.direction is GoalDirection.AT_LEAST else "at most"
    label = goal.account_name or goal.account_id
    suffix = f" by {goal.target_date.isoformat()}" if goal.target_date is not None else ""
    return f"{label} {direction} {goal.amount}{suffix}"


def describe_progress(progress: GoalProgress) -> str:
    """One-line progress/pace summary, shared by CLI and dashboard.

    The MCP tool does NOT use this — it returns the raw fields so
    Claude can phrase things itself.
    """
    goal = progress.goal
    if goal.kind is GoalKind.SPENDING_CAP:
        period_noun = "month" if goal.period is GoalPeriod.MONTH else "year"
        line = (
            f"{progress.current} of {progress.target} ({progress.percent}%) "
            f"this {period_noun} — {progress.period_elapsed_percent}% of period elapsed"
        )
        if progress.status is GoalStatus.AT_RISK:
            line += ", ahead of pace"
        elif progress.status is GoalStatus.OVER:
            line += ", over the cap"
        return line
    if goal.kind is GoalKind.CONTRIBUTION:
        period_noun = "month" if goal.period is GoalPeriod.MONTH else "year"
        line = (
            f"{progress.current} of {progress.target} ({progress.percent}%) "
            f"contributed this {period_noun} — {progress.period_elapsed_percent}% "
            "of period elapsed"
        )
        if progress.declared_total:
            # Disclose the calculated portion: declared schedule, not
            # observed in any feed.
            line += f", of which {progress.declared_total} declared recurring"
        if progress.status is GoalStatus.MET:
            line += ", met"
        elif progress.status is GoalStatus.AT_RISK:
            line += ", behind pace"
        if progress.pending_delta:
            line += f" ({progress.pending_delta:+} pending)"
        if progress.required_monthly is not None:
            line += f", need {progress.required_monthly}/mo"
        return line
    if goal.direction is GoalDirection.AT_MOST:
        line = f"{progress.current} vs ceiling {progress.target}"
    else:
        line = f"{progress.current} of {progress.target} ({progress.percent}%)"
    if progress.monthly_delta is not None:
        line += f" — {progress.monthly_delta:+}/mo avg"
    if progress.pending_delta:
        line += f" ({progress.pending_delta:+} pending)"
    if progress.projected_date is not None:
        line += f", projected {progress.projected_date.isoformat()}"
    if goal.target_date is not None:
        line += f" (target {goal.target_date.isoformat()}"
        if progress.required_monthly is not None:
            line += f", need {progress.required_monthly}/mo"
        line += ")"
    return line


__all__ = [
    "ContributionPeriod",
    "SpendingCapPeriod",
    "balance_goal_progress",
    "contribution_history",
    "contribution_monthly_target",
    "contribution_progress",
    "describe_goal",
    "describe_progress",
    "evaluate_goals",
    "goal_breach_warnings",
    "period_bounds",
    "spending_cap_history",
    "spending_cap_progress",
]
