"""Shared write-time validation for user-supplied rule patterns and goals.

Extracted from ``cli.py`` so the CLI (``category set-rule``) and the MCP
``add_category_rule`` tool call the exact same validator — the threat
model in CLAUDE.md ("category_rules.pattern is an MCP-reachable write
surface") assumes both surfaces are gated identically. The goal
validators follow the same contract: the CLI ``goal`` commands and the
MCP ``set_goal`` tool gate identically.

This module deliberately depends only on the stdlib. The CLI converts
:class:`RulePatternError` / :class:`GoalValidationError` into
``typer.BadParameter``; the MCP tool layer converts them into a
structured ``{ok: False, error: ...}`` result. Neither
exception-translation belongs here.
"""

from __future__ import annotations

import re
from datetime import date
from decimal import Decimal

PATTERN_MAX_LEN = 500

# Heuristic detectors for ReDoS-prone shapes. Caught at write time so the user
# gets a friendly error; the real runtime defense remains the query_sql
# statement-timeout watchdog (GOETTA_FINANCE_SQL_TIMEOUT_SECONDS, default 30s)
# which bounds any pattern that slips past these heuristics.
#
# Why heuristics, not a runtime timeout: CPython's ``re`` engine does NOT
# release the GIL during matching, so spawning a daemon thread to evaluate
# the pattern with a ``threading.Event.wait(timeout=1.0)`` does not actually
# bound execution — the main thread can't preempt the worker until the
# regex completes. Measured locally with ``(a+)+$`` against a 30-a sentinel:
# the daemon thread held the GIL for ~49 seconds while ``wait(1.0)`` was
# blocked the whole time. (See CLAUDE.md "Don'ts" → pattern surface.)
#
# Patterns the heuristics catch:
#   ``(X+)+``, ``(X*)*``, ``(X+)*``, ``(X*)+`` — nested quantifiers (the
#   classic backtracking shape)
#   ``{N,}`` / ``{N}`` with N > 10 — counted repetitions with overlap
#     potential
# Patterns they miss (by design, to keep false positives low):
#   ``(a|aa)+`` — alternation-overlap ReDoS; relies on runtime timeout
_NESTED_QUANTIFIER_RE = re.compile(r"\([^()]*[+*][^()]*\)[+*]")
_LARGE_REPETITION_RE = re.compile(r"\{\s*([0-9]+)\s*,?\s*[0-9]*\s*\}")
_LARGE_REPETITION_THRESHOLD = 10


class RulePatternError(ValueError):
    """A rule pattern failed write-time validation.

    ``param_hint`` mirrors typer.BadParameter's concept so the CLI can
    map it back onto the offending flag (``--pattern`` / ``--match``).
    """

    def __init__(self, message: str, *, param_hint: str = "--pattern") -> None:
        super().__init__(message)
        self.param_hint = param_hint


def parse_match_type(value: str) -> str:
    """Normalize and validate a match-type string ('contains' | 'regex')."""
    lowered = value.strip().lower()
    if lowered not in ("contains", "regex"):
        raise RulePatternError(
            f"match type must be 'contains' or 'regex', got {value!r}",
            param_hint="--match",
        )
    return lowered


def validate_rule_pattern(pattern: str, match_type: str) -> None:
    """Refuse syntactically invalid or heuristically ReDoS-prone patterns.

    Best-effort check at write time. The load-bearing runtime defense is
    the existing ``query_sql`` statement-timeout watchdog
    (GOETTA_FINANCE_SQL_TIMEOUT_SECONDS, default 30s), which bounds any
    pattern that slips past these heuristics. See CLAUDE.md "Don'ts" →
    rule-pattern surface for the threat model.

    Three layers, fastest first:
      1. Non-empty + length cap.
      2. For ``regex``: ``re.compile()`` — catches syntax errors.
      3. For ``regex``: heuristic shape detector — refuses obviously
         ReDoS-prone constructs (nested quantifiers; large counted
         repetitions). False-positive rate is low; the heuristic is
         conservative on purpose since legitimate finance-merchant
         patterns are short.

    Raises :class:`RulePatternError` on refusal.
    """
    if not pattern.strip():
        raise RulePatternError("pattern cannot be empty")
    if len(pattern) > PATTERN_MAX_LEN:
        raise RulePatternError(f"pattern is too long (>{PATTERN_MAX_LEN} chars)")
    if match_type == "contains":
        return
    if match_type != "regex":
        raise RulePatternError(
            f"match type must be 'contains' or 'regex', got {match_type!r}",
            param_hint="--match",
        )
    try:
        re.compile(pattern)
    except re.error as exc:
        raise RulePatternError(f"regex did not compile: {exc}") from exc

    if _NESTED_QUANTIFIER_RE.search(pattern):
        raise RulePatternError(
            "pattern contains a nested quantifier (e.g. (X+)+ or (X*)*); "
            "this is the canonical ReDoS shape and is refused. Rewrite "
            "without the outer quantifier or use 'contains' instead."
        )
    for match in _LARGE_REPETITION_RE.finditer(pattern):
        try:
            n = int(match.group(1))
        except (TypeError, ValueError):
            continue
        if n > _LARGE_REPETITION_THRESHOLD:
            raise RulePatternError(
                f"pattern uses a counted repetition with N={n} "
                f"(>{_LARGE_REPETITION_THRESHOLD}); refused as ReDoS-prone."
            )


# --- Goal validation (migration 0008) --------------------------------------

GOAL_NAME_MAX_LEN = 100
# Sanity bound well under DECIMAL(18,2)'s ceiling; nobody has a
# billion-dollar grocery cap, and a fat-fingered exponent should fail
# loudly at write time rather than produce a goal that can never move.
GOAL_AMOUNT_MAX = Decimal("1000000000")

_GOAL_KINDS = ("spending_cap", "balance")
_GOAL_PERIODS = ("month", "year")
_GOAL_DIRECTIONS = ("at_least", "at_most")


class GoalValidationError(ValueError):
    """A goal definition failed write-time validation.

    ``param_hint`` mirrors typer.BadParameter's concept so the CLI can
    map it back onto the offending flag.
    """

    def __init__(self, message: str, *, param_hint: str = "--amount") -> None:
        super().__init__(message)
        self.param_hint = param_hint


def validate_goal_name(name: str) -> str:
    """Strip and bound a goal name. Returns the stripped name."""
    stripped = name.strip()
    if not stripped:
        raise GoalValidationError("goal name cannot be empty", param_hint="--name")
    if len(stripped) > GOAL_NAME_MAX_LEN:
        raise GoalValidationError(
            f"goal name is too long (>{GOAL_NAME_MAX_LEN} chars)", param_hint="--name"
        )
    return stripped


def validate_goal_amount(amount: Decimal) -> None:
    """Finite, positive, at most GOAL_AMOUNT_MAX, no sub-cent precision."""
    if not amount.is_finite():
        raise GoalValidationError(f"amount must be a finite number, got {amount}")
    if amount <= 0:
        raise GoalValidationError(f"amount must be positive, got {amount}")
    if amount > GOAL_AMOUNT_MAX:
        raise GoalValidationError(f"amount is implausibly large (>{GOAL_AMOUNT_MAX}): {amount}")
    exponent = amount.as_tuple().exponent
    if isinstance(exponent, int) and exponent < -2:
        raise GoalValidationError(f"amount has sub-cent precision: {amount}")


def parse_goal_kind(value: str) -> str:
    lowered = value.strip().lower()
    if lowered not in _GOAL_KINDS:
        raise GoalValidationError(
            f"kind must be 'spending_cap' or 'balance', got {value!r}",
            param_hint="--kind",
        )
    return lowered


def parse_goal_period(value: str) -> str:
    lowered = value.strip().lower()
    if lowered not in _GOAL_PERIODS:
        raise GoalValidationError(
            f"period must be 'month' or 'year', got {value!r}",
            param_hint="--period",
        )
    return lowered


def parse_goal_direction(value: str) -> str:
    lowered = value.strip().lower()
    if lowered not in _GOAL_DIRECTIONS:
        raise GoalValidationError(
            f"direction must be 'at_least' or 'at_most', got {value!r}",
            param_hint="--direction",
        )
    return lowered


def parse_goal_target_date(value: str | None, *, today: date | None = None) -> date | None:
    """Parse an optional ISO target date; must be strictly in the future.

    Pace toward a past date is meaningless at creation time. Goals whose
    target_date passes after creation are handled by the progress math
    (they go at_risk while unmet), not rejected here.
    """
    if value is None:
        return None
    try:
        parsed = date.fromisoformat(value.strip())
    except ValueError as exc:
        raise GoalValidationError(
            f"target date must be YYYY-MM-DD, got {value!r}", param_hint="--by"
        ) from exc
    reference = today if today is not None else date.today()
    if parsed <= reference:
        raise GoalValidationError(
            f"target date must be in the future, got {parsed.isoformat()}",
            param_hint="--by",
        )
    return parsed


def validate_goal_shape(
    kind: str,
    *,
    category: str | None,
    period: str | None,
    account_id: str | None,
    direction: str | None,
    target_date: date | None,
) -> None:
    """Cross-field check mirroring the goals table's CHECK constraint.

    The CLI commands satisfy this by construction (each command only
    exposes its kind's flags); the MCP ``set_goal`` tool takes the full
    flat parameter set and needs it. The store re-enforces via the SQL
    CHECK as the backstop.
    """
    if kind == "spending_cap":
        if category is None or period is None:
            raise GoalValidationError(
                "spending_cap goals require a category and a period",
                param_hint="--category",
            )
        if account_id is not None or direction is not None or target_date is not None:
            raise GoalValidationError(
                "spending_cap goals do not take account_id, direction, or target_date",
                param_hint="--category",
            )
    elif kind == "balance":
        if account_id is None or direction is None:
            raise GoalValidationError(
                "balance goals require an account_id and a direction",
                param_hint="--direction",
            )
        if category is not None or period is not None:
            raise GoalValidationError(
                "balance goals do not take a category or period",
                param_hint="--direction",
            )
    else:
        raise GoalValidationError(
            f"kind must be 'spending_cap' or 'balance', got {kind!r}",
            param_hint="--kind",
        )
