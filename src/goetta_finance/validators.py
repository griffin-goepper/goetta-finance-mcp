"""Shared write-time validation for user-supplied rule patterns.

Extracted from ``cli.py`` so the CLI (``category set-rule``) and the MCP
``add_category_rule`` tool call the exact same validator — the threat
model in CLAUDE.md ("category_rules.pattern is an MCP-reachable write
surface") assumes both surfaces are gated identically.

This module deliberately depends only on the stdlib (``re``). The CLI
converts :class:`RulePatternError` into ``typer.BadParameter``; the MCP
tool layer converts it into a structured ``{ok: False, error: ...}``
result. Neither exception-translation belongs here.
"""

from __future__ import annotations

import re

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
