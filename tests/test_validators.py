"""Direct tests for the shared rule-pattern validator.

The CLI tests in test_category_cli.py exercise the same logic through
the typer surface; these pin the validator itself so the MCP
``add_category_rule`` path (which also calls it) inherits identical
coverage. Per CLAUDE.md: both write surfaces must be gated identically.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

import pytest

from goetta_finance.validators import (
    GOAL_NAME_MAX_LEN,
    PATTERN_MAX_LEN,
    RULE_AMOUNT_MAX,
    GoalValidationError,
    RulePatternError,
    format_rule_bounds,
    parse_goal_baseline_date,
    parse_goal_direction,
    parse_goal_kind,
    parse_goal_period,
    parse_goal_target_date,
    parse_match_type,
    parse_recurring_anchor,
    parse_recurring_interval,
    validate_goal_amount,
    validate_goal_baseline,
    validate_goal_name,
    validate_goal_recurring,
    validate_goal_shape,
    validate_rule_amount_bounds,
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


def test_validate_goal_amount_carries_custom_param_hint() -> None:
    """The CLI names the flag it actually exposes (--limit / --target)."""
    with pytest.raises(GoalValidationError) as exc_info:
        validate_goal_amount(Decimal("-1"), param_hint="--limit")
    assert exc_info.value.param_hint == "--limit"


def test_parse_goal_kind_normalizes_and_rejects() -> None:
    assert parse_goal_kind("  Spending_Cap ") == "spending_cap"
    assert parse_goal_kind("BALANCE") == "balance"
    assert parse_goal_kind("Contribution") == "contribution"
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


# --- Contribution goal validators (migration 0014) ---------------------------
# The match pattern goes through the SAME validate_rule_pattern as
# category rules and transfer links (MCP-reachable regex write surface —
# CLAUDE.md threat model); these pin the contribution-specific pieces.


def test_validate_goal_shape_contribution_happy() -> None:
    validate_goal_shape(
        "contribution",
        category=None,
        period="year",
        account_id="a1",
        direction=None,
        target_date=None,
        match_type="contains",
        match_pattern="CASH CONTRIBUTION",
        baseline_amount=Decimal("3000"),
        baseline_date=datetime(2026, 3, 1, tzinfo=UTC),
    )  # no raise
    validate_goal_shape(
        "contribution",
        category=None,
        period="month",
        account_id="a1",
        direction=None,
        target_date=None,
    )  # patternless + baselineless is a valid shape (manual accounts)


def test_validate_goal_shape_contribution_requirements_and_forbids() -> None:
    with pytest.raises(GoalValidationError, match="require an account_id and a period"):
        validate_goal_shape(
            "contribution",
            category=None,
            period=None,
            account_id="a1",
            direction=None,
            target_date=None,
        )
    with pytest.raises(GoalValidationError, match="do not take a category"):
        validate_goal_shape(
            "contribution",
            category="Dining",
            period="month",
            account_id="a1",
            direction=None,
            target_date=None,
        )
    with pytest.raises(GoalValidationError, match="do not take a category"):
        validate_goal_shape(
            "contribution",
            category=None,
            period="month",
            account_id="a1",
            direction="at_least",
            target_date=None,
        )
    with pytest.raises(GoalValidationError, match="provided together") as exc_info:
        validate_goal_shape(
            "contribution",
            category=None,
            period="month",
            account_id="a1",
            direction=None,
            target_date=None,
            match_type="contains",
        )
    assert exc_info.value.param_hint == "--pattern"


def test_validate_goal_shape_match_and_baseline_only_on_contribution() -> None:
    with pytest.raises(GoalValidationError, match="only apply to contribution"):
        validate_goal_shape(
            "spending_cap",
            category="Dining",
            period="month",
            account_id=None,
            direction=None,
            target_date=None,
            match_type="contains",
            match_pattern="X",
        )
    with pytest.raises(GoalValidationError, match="only apply to contribution"):
        validate_goal_shape(
            "balance",
            category=None,
            period=None,
            account_id="a1",
            direction="at_least",
            target_date=None,
            baseline_amount=Decimal("50"),
            baseline_date=datetime(2026, 3, 1, tzinfo=UTC),
        )


def test_goal_match_pattern_goes_through_rule_pattern_validator() -> None:
    """The contribution matcher is validated by the EXISTING shared
    validator — the canonical ReDoS shape is refused for goals exactly
    as for category rules."""
    with pytest.raises(RulePatternError, match="nested quantifier"):
        validate_rule_pattern("(a+)+$", "regex")


def test_validate_goal_baseline_pair_rules() -> None:
    validate_goal_baseline(None, None)  # no raise
    validate_goal_baseline(Decimal("3000"), datetime(2026, 3, 1, tzinfo=UTC))  # no raise
    with pytest.raises(GoalValidationError, match="provided together") as exc_info:
        validate_goal_baseline(Decimal("3000"), None)
    assert exc_info.value.param_hint == "--baseline"
    with pytest.raises(GoalValidationError, match="provided together"):
        validate_goal_baseline(None, datetime(2026, 3, 1, tzinfo=UTC))
    with pytest.raises(GoalValidationError, match="positive"):
        validate_goal_baseline(Decimal("-5"), datetime(2026, 3, 1, tzinfo=UTC))
    with pytest.raises(GoalValidationError, match="sub-cent"):
        validate_goal_baseline(Decimal("9.999"), datetime(2026, 3, 1, tzinfo=UTC))


def test_parse_recurring_interval_normalizes_and_rejects() -> None:
    assert parse_recurring_interval("  Biweekly ") == "biweekly"
    assert parse_recurring_interval("WEEKLY") == "weekly"
    assert parse_recurring_interval("monthly") == "monthly"
    with pytest.raises(GoalValidationError, match="'weekly', 'biweekly', or 'monthly'") as exc_info:
        parse_recurring_interval("fortnightly")
    assert exc_info.value.param_hint == "--recurring-interval"


def test_parse_recurring_anchor_past_allowed() -> None:
    """Unlike target dates, anchors in the past are the NORMAL case —
    the payday series extends both directions from the anchor."""
    assert parse_recurring_anchor(None) is None
    assert parse_recurring_anchor("2026-01-09") == date(2026, 1, 9)
    assert parse_recurring_anchor("2000-01-01") == date(2000, 1, 1)  # no future check
    with pytest.raises(GoalValidationError, match="YYYY-MM-DD") as exc_info:
        parse_recurring_anchor("Jan 9th")
    assert exc_info.value.param_hint == "--recurring-anchor"


def test_validate_goal_recurring_triple_rules() -> None:
    validate_goal_recurring(None, None, None)  # no raise
    validate_goal_recurring(Decimal("150.00"), "biweekly", date(2026, 1, 9))  # no raise
    for partial in (
        (Decimal("150.00"), None, None),
        (None, "biweekly", None),
        (None, None, date(2026, 1, 9)),
        (Decimal("150.00"), "biweekly", None),
    ):
        with pytest.raises(GoalValidationError, match="provided together") as exc_info:
            validate_goal_recurring(*partial)
        assert exc_info.value.param_hint == "--recurring"
    with pytest.raises(GoalValidationError, match="positive"):
        validate_goal_recurring(Decimal("-5"), "biweekly", date(2026, 1, 9))
    with pytest.raises(GoalValidationError, match="sub-cent"):
        validate_goal_recurring(Decimal("9.999"), "biweekly", date(2026, 1, 9))
    with pytest.raises(GoalValidationError, match="'weekly', 'biweekly', or 'monthly'"):
        validate_goal_recurring(Decimal("50"), "fortnightly", date(2026, 1, 9))


def test_validate_goal_shape_recurring_only_on_contribution() -> None:
    with pytest.raises(GoalValidationError, match="only apply to contribution"):
        validate_goal_shape(
            "balance",
            category=None,
            period=None,
            account_id="a1",
            direction="at_least",
            target_date=None,
            recurring_amount=Decimal("50"),
            recurring_interval="biweekly",
            recurring_anchor=date(2026, 1, 9),
        )
    # Contribution accepts the full triple.
    validate_goal_shape(
        "contribution",
        category=None,
        period="year",
        account_id="a1",
        direction=None,
        target_date=None,
        recurring_amount=Decimal("150.00"),
        recurring_interval="biweekly",
        recurring_anchor=date(2026, 1, 9),
    )  # no raise
    with pytest.raises(GoalValidationError, match="provided together"):
        validate_goal_shape(
            "contribution",
            category=None,
            period="year",
            account_id="a1",
            direction=None,
            target_date=None,
            recurring_amount=Decimal("150.00"),
        )


def test_parse_goal_baseline_date_parses_and_bounds() -> None:
    now = datetime(2026, 5, 13, 12, tzinfo=UTC)
    assert parse_goal_baseline_date(None, now=now) is None
    assert parse_goal_baseline_date("2026-03-01", now=now) == datetime(2026, 3, 1, tzinfo=UTC)
    # Full datetimes work too; naive is taken as UTC.
    assert parse_goal_baseline_date("2026-03-01T09:30:00", now=now) == datetime(
        2026, 3, 1, 9, 30, tzinfo=UTC
    )
    with pytest.raises(GoalValidationError, match="future") as exc_info:
        parse_goal_baseline_date("2026-05-14", now=now)
    assert exc_info.value.param_hint == "--baseline-date"
    with pytest.raises(GoalValidationError, match="ISO"):
        parse_goal_baseline_date("March 1st", now=now)


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


# --- Rule amount bounds (migration 0009) -------------------------------------


@pytest.mark.parametrize(
    ("min_amount", "max_amount"),
    [
        (None, None),
        (None, Decimal("20")),
        (Decimal("10"), None),
        (Decimal("0.01"), RULE_AMOUNT_MAX),
        (Decimal("19.99"), Decimal("20.00")),  # adjacent cents are a valid interval
    ],
)
def test_validate_rule_amount_bounds_accepts(
    min_amount: Decimal | None, max_amount: Decimal | None
) -> None:
    validate_rule_amount_bounds(min_amount, max_amount)  # no raise


@pytest.mark.parametrize(
    ("min_amount", "max_amount", "message", "hint"),
    [
        (Decimal("0"), None, "positive", "--min-amount"),
        (Decimal("-5"), None, "positive", "--min-amount"),
        (None, Decimal("0"), "positive", "--max-amount"),
        (Decimal("9.999"), None, "sub-cent", "--min-amount"),
        (None, Decimal("19.999"), "sub-cent", "--max-amount"),
        (Decimal("NaN"), None, "finite", "--min-amount"),
        (None, Decimal("Infinity"), "finite", "--max-amount"),
        (None, RULE_AMOUNT_MAX + Decimal("0.01"), "implausibly large", "--max-amount"),
    ],
)
def test_validate_rule_amount_bounds_rejections(
    min_amount: Decimal | None, max_amount: Decimal | None, message: str, hint: str
) -> None:
    with pytest.raises(RulePatternError, match=message) as exc_info:
        validate_rule_amount_bounds(min_amount, max_amount)
    assert exc_info.value.param_hint == hint


@pytest.mark.parametrize(
    ("min_amount", "max_amount"),
    [
        (Decimal("20"), Decimal("20")),  # equal bounds match nothing in [min, max)
        (Decimal("30"), Decimal("20")),
    ],
)
def test_validate_rule_amount_bounds_rejects_min_not_below_max(
    min_amount: Decimal, max_amount: Decimal
) -> None:
    with pytest.raises(RulePatternError, match="strictly below") as exc_info:
        validate_rule_amount_bounds(min_amount, max_amount)
    assert exc_info.value.param_hint == "--min-amount"


def test_format_rule_bounds_shapes() -> None:
    """All four shapes, ASCII only — CLI output must survive cp1252."""
    assert format_rule_bounds(None, None) == ""
    assert format_rule_bounds(None, Decimal("20")) == "|amount| < $20.00"
    assert format_rule_bounds(Decimal("20"), None) == "|amount| >= $20.00"
    assert format_rule_bounds(Decimal("10"), Decimal("20")) == "$10.00 <= |amount| < $20.00"
    combined = format_rule_bounds(Decimal("1234.5"), Decimal("2000"))
    assert combined == "$1,234.50 <= |amount| < $2,000.00"
    assert combined.isascii()
