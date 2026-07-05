"""Direct tests for the shared rule-pattern validator.

The CLI tests in test_category_cli.py exercise the same logic through
the typer surface; these pin the validator itself so the MCP
``add_category_rule`` path (which also calls it) inherits identical
coverage. Per CLAUDE.md: both write surfaces must be gated identically.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from goetta_finance.validators import (
    GOAL_NAME_MAX_LEN,
    PATTERN_MAX_LEN,
    GoalValidationError,
    RulePatternError,
    parse_goal_direction,
    parse_goal_kind,
    parse_goal_period,
    parse_goal_target_date,
    parse_match_type,
    validate_goal_amount,
    validate_goal_name,
    validate_goal_shape,
    validate_rule_pattern,
)


def test_parse_match_type_normalizes_case() -> None:
    assert parse_match_type("Contains") == "contains"
    assert parse_match_type("  REGEX  ") == "regex"


def test_parse_match_type_rejects_unknown() -> None:
    with pytest.raises(RulePatternError, match=r"contains.*regex"):
        parse_match_type("exact")


def test_validate_accepts_plain_contains() -> None:
    validate_rule_pattern("KROGER", "contains")  # no raise


def test_validate_accepts_legitimate_regex() -> None:
    validate_rule_pattern("(?i)venmo", "regex")  # no raise


def test_validate_rejects_empty_pattern() -> None:
    with pytest.raises(RulePatternError, match="empty"):
        validate_rule_pattern("   ", "contains")


def test_validate_rejects_overlong_pattern() -> None:
    with pytest.raises(RulePatternError, match="too long"):
        validate_rule_pattern("x" * (PATTERN_MAX_LEN + 1), "contains")


def test_validate_rejects_uncompilable_regex() -> None:
    with pytest.raises(RulePatternError, match="did not compile"):
        validate_rule_pattern("[", "regex")


@pytest.mark.parametrize("pattern", ["(a+)+", "(a+)+$", "(x*)*", "(b+)*", "(c*)+"])
def test_validate_rejects_nested_quantifiers(pattern: str) -> None:
    """The canonical ReDoS shape. CPython's re engine doesn't release the
    GIL during matching so a runtime timeout is impossible — the heuristic
    refusal at write time is the control (see CLAUDE.md)."""
    with pytest.raises(RulePatternError, match="nested quantifier"):
        validate_rule_pattern(pattern, "regex")


def test_validate_rejects_large_counted_repetition() -> None:
    with pytest.raises(RulePatternError, match="counted repetition"):
        validate_rule_pattern("(.*a){25}", "regex")


def test_validate_allows_small_counted_repetition() -> None:
    validate_rule_pattern("a{3}b", "regex")  # no raise


def test_contains_pattern_skips_regex_checks() -> None:
    """'contains' patterns are substring matches — regex-syntax characters
    in them are literal and must not be rejected."""
    validate_rule_pattern("Affirm * Pay", "contains")  # no raise
    validate_rule_pattern("(a+)+", "contains")  # would be refused as regex


def test_rule_pattern_error_carries_param_hint() -> None:
    """The CLI maps param_hint back onto the offending flag."""
    with pytest.raises(RulePatternError) as exc_info:
        parse_match_type("nope")
    assert exc_info.value.param_hint == "--match"
    with pytest.raises(RulePatternError) as exc_info:
        validate_rule_pattern("", "contains")
    assert exc_info.value.param_hint == "--pattern"


# --- Goal validators (migration 0008 slice) ---------------------------------
# Same contract as the rule-pattern validator: the CLI `goal` commands
# and the MCP `set_goal` tool call these identically.


def test_validate_goal_name_strips_and_returns() -> None:
    assert validate_goal_name("  Groceries cap  ") == "Groceries cap"


def test_validate_goal_name_rejects_empty_and_overlong() -> None:
    with pytest.raises(GoalValidationError, match="empty") as exc_info:
        validate_goal_name("   ")
    assert exc_info.value.param_hint == "--name"
    with pytest.raises(GoalValidationError, match="too long"):
        validate_goal_name("x" * (GOAL_NAME_MAX_LEN + 1))


@pytest.mark.parametrize("amount", ["400", "0.01", "1000000000"])
def test_validate_goal_amount_accepts_boundaries(amount: str) -> None:
    validate_goal_amount(Decimal(amount))  # no raise


@pytest.mark.parametrize(
    ("amount", "message"),
    [
        ("0", "positive"),
        ("-5", "positive"),
        ("1000000000.01", "implausibly large"),
        ("9.999", "sub-cent"),
        ("NaN", "finite"),
        ("Infinity", "finite"),
    ],
)
def test_validate_goal_amount_rejections(amount: str, message: str) -> None:
    with pytest.raises(GoalValidationError, match=message):
        validate_goal_amount(Decimal(amount))


def test_parse_goal_kind_normalizes_and_rejects() -> None:
    assert parse_goal_kind("  Spending_Cap ") == "spending_cap"
    assert parse_goal_kind("BALANCE") == "balance"
    with pytest.raises(GoalValidationError) as exc_info:
        parse_goal_kind("envelope")
    assert exc_info.value.param_hint == "--kind"


def test_parse_goal_period_normalizes_and_rejects() -> None:
    assert parse_goal_period("Month") == "month"
    assert parse_goal_period("YEAR") == "year"
    with pytest.raises(GoalValidationError) as exc_info:
        parse_goal_period("week")
    assert exc_info.value.param_hint == "--period"


def test_parse_goal_direction_normalizes_and_rejects() -> None:
    assert parse_goal_direction("At_Least") == "at_least"
    assert parse_goal_direction("AT_MOST") == "at_most"
    with pytest.raises(GoalValidationError) as exc_info:
        parse_goal_direction("exactly")
    assert exc_info.value.param_hint == "--direction"


def test_parse_goal_target_date_none_passthrough() -> None:
    assert parse_goal_target_date(None) is None


def test_parse_goal_target_date_future_ok() -> None:
    today = date(2026, 5, 13)
    assert parse_goal_target_date("2027-06-01", today=today) == date(2027, 6, 1)


@pytest.mark.parametrize("value", ["2026-05-13", "2026-01-01"])
def test_parse_goal_target_date_rejects_today_and_past(value: str) -> None:
    with pytest.raises(GoalValidationError, match="future") as exc_info:
        parse_goal_target_date(value, today=date(2026, 5, 13))
    assert exc_info.value.param_hint == "--by"


def test_parse_goal_target_date_rejects_garbage() -> None:
    with pytest.raises(GoalValidationError, match="YYYY-MM-DD"):
        parse_goal_target_date("June 1st 2027", today=date(2026, 5, 13))


def test_validate_goal_shape_spending_cap() -> None:
    validate_goal_shape(
        "spending_cap",
        category="Dining",
        period="month",
        account_id=None,
        direction=None,
        target_date=None,
    )  # no raise
    with pytest.raises(GoalValidationError, match="require a category"):
        validate_goal_shape(
            "spending_cap",
            category=None,
            period="month",
            account_id=None,
            direction=None,
            target_date=None,
        )
    with pytest.raises(GoalValidationError, match="do not take account_id"):
        validate_goal_shape(
            "spending_cap",
            category="Dining",
            period="month",
            account_id="a1",
            direction=None,
            target_date=None,
        )


def test_validate_goal_shape_balance() -> None:
    validate_goal_shape(
        "balance",
        category=None,
        period=None,
        account_id="a1",
        direction="at_least",
        target_date=date(2027, 6, 1),
    )  # no raise
    with pytest.raises(GoalValidationError, match="require an account_id"):
        validate_goal_shape(
            "balance",
            category=None,
            period=None,
            account_id="a1",
            direction=None,
            target_date=None,
        )
    with pytest.raises(GoalValidationError, match="do not take a category"):
        validate_goal_shape(
            "balance",
            category="Dining",
            period=None,
            account_id="a1",
            direction="at_most",
            target_date=None,
        )


def test_validate_goal_shape_unknown_kind() -> None:
    with pytest.raises(GoalValidationError, match="kind must be"):
        validate_goal_shape(
            "envelope",
            category=None,
            period=None,
            account_id=None,
            direction=None,
            target_date=None,
        )
