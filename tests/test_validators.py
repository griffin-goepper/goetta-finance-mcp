"""Direct tests for the shared rule-pattern validator.

The CLI tests in test_category_cli.py exercise the same logic through
the typer surface; these pin the validator itself so the MCP
``add_category_rule`` path (which also calls it) inherits identical
coverage. Per CLAUDE.md: both write surfaces must be gated identically.
"""

from __future__ import annotations

import pytest

from goetta_finance.validators import (
    PATTERN_MAX_LEN,
    RulePatternError,
    parse_match_type,
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
