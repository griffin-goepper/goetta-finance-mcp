"""MCP surface for goals: list with computed progress, create, delete.

Write functions return structured ``{ok: bool, ...}`` results instead
of exceptions — MCP tool results should be model-readable outcomes,
not stack traces (same contract as ``tools/categorize.py``).

``set_goal`` calls the same ``validators`` functions as the CLI's
``goal add-*`` commands so both write surfaces are gated identically,
mirroring the rule-pattern precedent.

``list_goals`` returns raw fields (money as strings, dates as ISO)
rather than prose so Claude can phrase pace itself; the shared prose
formatters (``goals.describe_*``) are for the CLI and dashboard.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from goetta_finance.errors import StoreError
from goetta_finance.goals import evaluate_goals
from goetta_finance.store import FinanceStore
from goetta_finance.tools._serialize import serialize_value
from goetta_finance.tools.categorize import _suggest_category
from goetta_finance.validators import (
    GoalValidationError,
    RulePatternError,
    parse_goal_baseline_date,
    parse_goal_direction,
    parse_goal_kind,
    parse_goal_period,
    parse_goal_target_date,
    parse_match_type,
    validate_goal_amount,
    validate_goal_baseline,
    validate_goal_name,
    validate_goal_shape,
    validate_rule_pattern,
)


def list_goals(store: FinanceStore) -> list[dict[str, Any]]:
    """Every goal with progress computed fresh at call time."""
    out: list[dict[str, Any]] = []
    for progress in evaluate_goals(store):
        goal = progress.goal
        out.append(
            {
                "id": goal.id,
                "name": goal.name,
                "kind": goal.kind.value,
                "amount": serialize_value(goal.amount),
                "category": goal.category_name,
                "period": goal.period.value if goal.period is not None else None,
                "account_id": goal.account_id,
                "account_name": goal.account_name,
                "direction": goal.direction.value if goal.direction is not None else None,
                "target_date": serialize_value(goal.target_date),
                "status": progress.status.value,
                "current": serialize_value(progress.current),
                "target": serialize_value(progress.target),
                "percent": serialize_value(progress.percent),
                "period_start": serialize_value(progress.period_start),
                "period_end": serialize_value(progress.period_end),
                "period_elapsed_percent": serialize_value(progress.period_elapsed_percent),
                "monthly_delta": serialize_value(progress.monthly_delta),
                "required_monthly": serialize_value(progress.required_monthly),
                "projected_date": serialize_value(progress.projected_date),
                "pending_delta": serialize_value(progress.pending_delta),
                # Contribution-goal definition fields (migration 0014);
                # null on every other kind — the wire shape is uniform.
                "match_type": goal.match_type,
                "match_pattern": goal.match_pattern,
                "baseline_amount": serialize_value(goal.baseline_amount),
                "baseline_date": serialize_value(goal.baseline_date),
            }
        )
    return out


def set_goal(
    store: FinanceStore,
    *,
    name: str,
    kind: str,
    amount: Decimal,
    category: str | None = None,
    period: str | None = None,
    account_id: str | None = None,
    direction: str | None = None,
    target_date: str | None = None,
    match_type: str | None = None,
    match_pattern: str | None = None,
    baseline_amount: Decimal | None = None,
    baseline_date: str | None = None,
) -> dict[str, Any]:
    """Create a goal. Validates first (shared validators), then writes.

    ``match_pattern`` is an MCP-reachable regex write surface (the goal
    pattern runs against every future feed row), so it goes through the
    SAME ``validate_rule_pattern`` as category rules and transfer
    links — identical gating to the CLI. ``match_type`` defaults to
    'contains' when a pattern is given without one, mirroring the CLI
    flag default.
    """
    try:
        normalized_kind = parse_goal_kind(kind)
        goal_name = validate_goal_name(name)
        validate_goal_amount(amount)
        normalized_period = parse_goal_period(period) if period is not None else None
        normalized_direction = parse_goal_direction(direction) if direction is not None else None
        parsed_target_date = parse_goal_target_date(target_date)
        normalized_match_type: str | None = None
        if match_pattern is not None:
            normalized_match_type = parse_match_type(match_type if match_type else "contains")
            validate_rule_pattern(match_pattern, normalized_match_type)
        elif match_type is not None:
            raise GoalValidationError("match_type requires a match_pattern", param_hint="--pattern")
        parsed_baseline_date = parse_goal_baseline_date(baseline_date)
        validate_goal_baseline(baseline_amount, parsed_baseline_date)
        validate_goal_shape(
            normalized_kind,
            category=category,
            period=normalized_period,
            account_id=account_id,
            direction=normalized_direction,
            target_date=parsed_target_date,
            match_type=normalized_match_type,
            match_pattern=match_pattern,
            baseline_amount=baseline_amount,
            baseline_date=parsed_baseline_date,
        )
    except (GoalValidationError, RulePatternError) as exc:
        return {"ok": False, "error": f"goal validation failed: {exc}"}
    try:
        goal = store.add_goal(
            goal_name,
            kind=normalized_kind,
            amount=amount,
            category_name=category,
            period=normalized_period,
            account_id=account_id,
            direction=normalized_direction,
            target_date=parsed_target_date,
            match_type=normalized_match_type,
            match_pattern=match_pattern,
            baseline_amount=baseline_amount,
            baseline_date=parsed_baseline_date,
        )
    except StoreError as exc:
        message = str(exc)
        if "category not found" in message.lower() and category is not None:
            message += _suggest_category(store, category)
        return {"ok": False, "error": message}
    return {
        "ok": True,
        "goal_id": goal.id,
        "message": f'Created goal {goal.id} "{goal.name}". Progress is computed '
        "at read time — call list_goals to see current status.",
    }


def remove_goal(store: FinanceStore, goal_id: int) -> dict[str, Any]:
    """Delete a goal by id."""
    try:
        store.remove_goal(goal_id)
    except StoreError as exc:
        return {"ok": False, "error": str(exc)}
    return {"ok": True, "message": f"Removed goal {goal_id}."}
