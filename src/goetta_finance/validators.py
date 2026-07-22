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
from datetime import UTC, date, datetime
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
    """A rule definition (pattern or amount bounds) failed write-time validation.

    ``param_hint`` mirrors typer.BadParameter's concept so the CLI can
    map it back onto the offending flag (``--pattern`` / ``--match`` /
    ``--min-amount`` / ``--max-amount``).
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

_GOAL_KINDS = ("spending_cap", "balance", "contribution")
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


def validate_goal_amount(amount: Decimal, *, param_hint: str = "--amount") -> None:
    """Finite, positive, at most GOAL_AMOUNT_MAX, no sub-cent precision.

    ``param_hint`` lets the CLI name the flag it actually exposes
    (``--limit`` on add-spending, ``--target`` on add-balance).
    """
    if not amount.is_finite():
        raise GoalValidationError(
            f"amount must be a finite number, got {amount}", param_hint=param_hint
        )
    if amount <= 0:
        raise GoalValidationError(f"amount must be positive, got {amount}", param_hint=param_hint)
    if amount > GOAL_AMOUNT_MAX:
        raise GoalValidationError(
            f"amount is implausibly large (>{GOAL_AMOUNT_MAX}): {amount}", param_hint=param_hint
        )
    exponent = amount.as_tuple().exponent
    if isinstance(exponent, int) and exponent < -2:
        raise GoalValidationError(f"amount has sub-cent precision: {amount}", param_hint=param_hint)


def parse_goal_kind(value: str) -> str:
    lowered = value.strip().lower()
    if lowered not in _GOAL_KINDS:
        raise GoalValidationError(
            f"kind must be 'spending_cap', 'balance', or 'contribution', got {value!r}",
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


def parse_goal_baseline_date(value: str | None, *, now: datetime | None = None) -> datetime | None:
    """Parse an optional ISO baseline date/datetime to tz-aware UTC.

    A date-only value becomes midnight UTC of that day; naive datetimes
    are taken as UTC (the storage convention). Must not be in the
    future — a baseline records contributions already made, so it lands
    in a real period. ``now`` is injectable for tests.
    """
    if value is None:
        return None
    text = value.strip()
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise GoalValidationError(
            f"baseline date must be ISO format (YYYY-MM-DD), got {value!r}",
            param_hint="--baseline-date",
        ) from exc
    parsed = parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)
    reference = now if now is not None else datetime.now(tz=UTC)
    if parsed > reference:
        raise GoalValidationError(
            f"baseline date cannot be in the future, got {text}",
            param_hint="--baseline-date",
        )
    return parsed


_RECURRING_INTERVALS = ("weekly", "biweekly", "monthly")


def parse_recurring_interval(value: str) -> str:
    """Normalize and validate a recurring interval
    ('weekly' | 'biweekly' | 'monthly')."""
    lowered = value.strip().lower()
    if lowered not in _RECURRING_INTERVALS:
        raise GoalValidationError(
            f"recurring interval must be 'weekly', 'biweekly', or 'monthly', got {value!r}",
            param_hint="--recurring-interval",
        )
    return lowered


def parse_recurring_anchor(value: str | None) -> date | None:
    """Parse an optional ISO recurring-anchor date.

    Past anchors are ALLOWED (unlike target dates): the payday series
    extends both directions from the anchor, so "first payday of the
    year" is the natural way to declare a schedule mid-year. Future
    anchors are allowed for the same reason.
    """
    if value is None:
        return None
    try:
        return date.fromisoformat(value.strip())
    except ValueError as exc:
        raise GoalValidationError(
            f"recurring anchor must be YYYY-MM-DD, got {value!r}",
            param_hint="--recurring-anchor",
        ) from exc


def validate_goal_recurring(
    recurring_amount: Decimal | None,
    recurring_interval: str | None,
    recurring_anchor: date | None,
) -> None:
    """Recurring-schedule triple consistency for contribution goals.

    All-three-or-none (an amount without a schedule, or a schedule
    without an amount, accrues nothing meaningful); the amount passes
    the same sanity bounds as goal amounts; the interval is whitelisted.
    Application-enforced only — migration 0015 is plain ALTERs with no
    table CHECK — so this and ``DuckDBStore.add_goal`` are the gates.
    """
    fields = (recurring_amount, recurring_interval, recurring_anchor)
    if any(f is not None for f in fields) and any(f is None for f in fields):
        raise GoalValidationError(
            "recurring amount, interval, and anchor must be provided together",
            param_hint="--recurring",
        )
    if recurring_amount is not None:
        validate_goal_amount(recurring_amount, param_hint="--recurring")
    if recurring_interval is not None and recurring_interval not in _RECURRING_INTERVALS:
        raise GoalValidationError(
            f"recurring interval must be 'weekly', 'biweekly', or 'monthly', "
            f"got {recurring_interval!r}",
            param_hint="--recurring-interval",
        )


def validate_goal_baseline(baseline_amount: Decimal | None, baseline_date: datetime | None) -> None:
    """Baseline pair consistency for contribution goals.

    Both-or-neither (a baseline amount is meaningless without the
    period it lands in, and vice versa); the amount passes the same
    sanity bounds as goal amounts. The not-in-the-future rule lives in
    :func:`parse_goal_baseline_date` — both surfaces parse through it.
    """
    if (baseline_amount is None) != (baseline_date is None):
        raise GoalValidationError(
            "baseline amount and baseline date must be provided together",
            param_hint="--baseline",
        )
    if baseline_amount is not None:
        validate_goal_amount(baseline_amount, param_hint="--baseline")


def validate_goal_shape(
    kind: str,
    *,
    category: str | None,
    period: str | None,
    account_id: str | None,
    direction: str | None,
    target_date: date | None,
    match_type: str | None = None,
    match_pattern: str | None = None,
    baseline_amount: Decimal | None = None,
    baseline_date: datetime | None = None,
    recurring_amount: Decimal | None = None,
    recurring_interval: str | None = None,
    recurring_anchor: date | None = None,
) -> None:
    """Cross-field check mirroring the goals table's CHECK constraints
    (plus the 0015 recurring rules, which have NO table CHECK — DuckDB
    can't ALTER one in; this and the store are the write-time gates).

    The CLI commands satisfy this by construction (each command only
    exposes its kind's flags); the MCP ``set_goal`` tool takes the full
    flat parameter set and needs it. The store re-enforces via the SQL
    CHECK as the backstop.
    """
    if kind != "contribution":
        if match_type is not None or match_pattern is not None:
            raise GoalValidationError(
                "match_type/match_pattern only apply to contribution goals",
                param_hint="--pattern",
            )
        if baseline_amount is not None or baseline_date is not None:
            raise GoalValidationError(
                "baseline_amount/baseline_date only apply to contribution goals",
                param_hint="--baseline",
            )
        if (
            recurring_amount is not None
            or recurring_interval is not None
            or recurring_anchor is not None
        ):
            raise GoalValidationError(
                "recurring contributions only apply to contribution goals",
                param_hint="--recurring",
            )
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
    elif kind == "contribution":
        if account_id is None or period is None:
            raise GoalValidationError(
                "contribution goals require an account_id and a period",
                param_hint="--period",
            )
        if category is not None or direction is not None or target_date is not None:
            raise GoalValidationError(
                "contribution goals do not take a category, direction, or target_date",
                param_hint="--period",
            )
        if (match_type is None) != (match_pattern is None):
            raise GoalValidationError(
                "match_type and match_pattern must be provided together",
                param_hint="--pattern",
            )
        validate_goal_baseline(baseline_amount, baseline_date)
        validate_goal_recurring(recurring_amount, recurring_interval, recurring_anchor)
    else:
        raise GoalValidationError(
            f"kind must be 'spending_cap', 'balance', or 'contribution', got {kind!r}",
            param_hint="--kind",
        )


# --- Rule amount bounds (migration 0009) ------------------------------------

# Same sanity ceiling as goals; a rule bound past a billion dollars is a typo.
RULE_AMOUNT_MAX = GOAL_AMOUNT_MAX


def validate_rule_amount_bounds(min_amount: Decimal | None, max_amount: Decimal | None) -> None:
    """Optional refinement bounds on a rule; compared against abs(amount).

    Each bound, when present: finite, > 0, <= RULE_AMOUNT_MAX, no
    sub-cent precision. When both present: min strictly below max — the
    view matches the half-open interval [min, max), so equal bounds
    would match nothing. None = unbounded on that side.

    Raises :class:`RulePatternError` with the offending flag as
    ``param_hint``.
    """
    for value, hint in ((min_amount, "--min-amount"), (max_amount, "--max-amount")):
        if value is None:
            continue
        if not value.is_finite():
            raise RulePatternError(
                f"amount bound must be a finite number, got {value}", param_hint=hint
            )
        if value <= 0:
            raise RulePatternError(f"amount bound must be positive, got {value}", param_hint=hint)
        if value > RULE_AMOUNT_MAX:
            raise RulePatternError(
                f"amount bound is implausibly large (>{RULE_AMOUNT_MAX}): {value}",
                param_hint=hint,
            )
        exponent = value.as_tuple().exponent
        if isinstance(exponent, int) and exponent < -2:
            raise RulePatternError(f"amount bound has sub-cent precision: {value}", param_hint=hint)
    if min_amount is not None and max_amount is not None and min_amount >= max_amount:
        raise RulePatternError(
            f"min_amount must be strictly below max_amount (the bounds form a "
            f"half-open interval [min, max)), got {min_amount} >= {max_amount}",
            param_hint="--min-amount",
        )


def format_rule_bounds(min_amount: Decimal | None, max_amount: Decimal | None) -> str:
    """Compact ASCII description of a rule's amount bounds; '' when unbounded.

    Lives here (stdlib-only, shared) so the CLI echoes and the MCP tool
    messages can't drift — the goals ``describe_*`` precedent. ASCII only:
    CLI output must survive cp1252 consoles.
    """
    if min_amount is not None and max_amount is not None:
        return f"${min_amount:,.2f} <= |amount| < ${max_amount:,.2f}"
    if min_amount is not None:
        return f"|amount| >= ${min_amount:,.2f}"
    if max_amount is not None:
        return f"|amount| < ${max_amount:,.2f}"
    return ""
