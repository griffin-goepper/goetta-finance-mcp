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
"""

from __future__ import annotations

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
from goetta_finance.tools.spending_by_category import query_spending_by_category

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


def spending_cap_progress(
    store: FinanceStore, goal: Goal, *, now: datetime | None = None
) -> GoalProgress:
    """Net spending in the goal's category this period vs the cap.

    ``include_non_spending=True`` so a cap on a category the user later
    flips to non-spending still returns a row — the total expression is
    identical in both modes; the flag only gates which rows appear.
    """
    if goal.period is None or goal.category_name is None:  # pragma: no cover
        raise StoreError(f"spending_cap goal {goal.id} is missing category or period")
    now = (now or datetime.now(tz=UTC)).astimezone(UTC)
    start, end = period_bounds(goal.period, now)
    # The shared helper's end bound is inclusive (posted <= end); back off
    # one microsecond so the first instant of the next bucket is excluded.
    rows = query_spending_by_category(
        store, start, end - timedelta(microseconds=1), include_non_spending=True
    )
    current = next(
        (_as_decimal(r["total"]) for r in rows if r["category"] == goal.category_name),
        Decimal("0"),
    )
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
    backwards from the bucket containing ``now``. Each bucket reuses the
    exact :func:`spending_cap_progress` query (same net-spending CASE,
    pending included, hidden excluded, microsecond end backoff), so the
    newest bucket always equals the goal card's ``current`` to the cent.
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
    out: list[SpendingCapPeriod] = []
    for start, end in reversed(bounds):
        rows = query_spending_by_category(
            store, start, end - timedelta(microseconds=1), include_non_spending=True
        )
        actual = next(
            (_as_decimal(r["total"]) for r in rows if r["category"] == goal.category_name),
            Decimal("0"),
        )
        out.append(
            SpendingCapPeriod(
                period_start=start,
                period_end=end,
                actual=actual,
                over=actual >= goal.amount,
            )
        )
    return out


def balance_goal_progress(
    goal: Goal,
    account: Account,
    snapshots: list[BalanceSnapshot],
    *,
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
    approaching the target regardless of direction.
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
            days_remaining = (goal.target_date - now.date()).days
            if days_remaining > 0:
                required_monthly = (gap / (Decimal(days_remaining) / _DAYS_PER_MONTH)).quantize(
                    _MONEY_Q
                )
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

    return GoalProgress(
        goal=goal,
        status=status,
        current=current,
        target=goal.amount,
        percent=percent,
        monthly_delta=monthly_delta,
        required_monthly=required_monthly,
        projected_date=projected_date,
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
    out: list[GoalProgress] = []
    for goal in goals:
        if goal.kind is GoalKind.SPENDING_CAP:
            out.append(spending_cap_progress(store, goal, now=now))
        else:
            if goal.account_id is None:  # pragma: no cover - table CHECK
                raise StoreError(f"balance goal {goal.id} has no account_id")
            account = accounts.get(goal.account_id)
            if account is None:
                raise StoreError(f"account not found: {goal.account_id}")
            snapshots = store.get_balance_history(
                goal.account_id, since=now - timedelta(days=_TREND_LOOKBACK_DAYS)
            )
            out.append(balance_goal_progress(goal, account, snapshots, now=now))
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
    if goal.direction is GoalDirection.AT_MOST:
        line = f"{progress.current} vs ceiling {progress.target}"
    else:
        line = f"{progress.current} of {progress.target} ({progress.percent}%)"
    if progress.monthly_delta is not None:
        line += f" — {progress.monthly_delta:+}/mo avg"
    if progress.projected_date is not None:
        line += f", projected {progress.projected_date.isoformat()}"
    if goal.target_date is not None:
        line += f" (target {goal.target_date.isoformat()}"
        if progress.required_monthly is not None:
            line += f", need {progress.required_monthly}/mo"
        line += ")"
    return line


__all__ = [
    "SpendingCapPeriod",
    "balance_goal_progress",
    "describe_goal",
    "describe_progress",
    "evaluate_goals",
    "goal_breach_warnings",
    "period_bounds",
    "spending_cap_history",
    "spending_cap_progress",
]
