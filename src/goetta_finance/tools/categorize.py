"""MCP write surface for categorization curation.

Four pure functions wrapping store methods with structured
``{ok: bool, ...}`` results instead of exceptions — MCP tool results
should be model-readable outcomes, not stack traces. The difflib
"did you mean?" suggestion from the CLI carries over so Claude can
self-correct a typo'd category name in the next call.

``add_category_rule`` calls the same ``validators.validate_rule_pattern``
as the CLI's ``category set-rule`` — the CLAUDE.md threat model
("category_rules.pattern is an MCP-reachable write surface") requires
both surfaces to be gated identically.
"""

from __future__ import annotations

import difflib
from decimal import Decimal
from typing import Any

from goetta_finance.errors import StoreError
from goetta_finance.store import FinanceStore
from goetta_finance.validators import (
    RulePatternError,
    format_rule_bounds,
    parse_match_type,
    validate_rule_amount_bounds,
    validate_rule_pattern,
)


def _suggest_category(store: FinanceStore, user_input: str) -> str:
    """Mirror of the CLI's typo helper, returning a suffix string."""
    names = [c.name for c in store.get_categories()]
    matches = difflib.get_close_matches(user_input, names, n=1, cutoff=0.6)
    if matches:
        return f' Did you mean "{matches[0]}"?'
    return " Call list_accounts-style discovery or sql_query on `categories` for valid names."


def categorize_transaction(
    store: FinanceStore, transaction_id: str, category_name: str
) -> dict[str, Any]:
    """Apply a manual per-transaction category override."""
    try:
        was_pending = store.set_transaction_override(transaction_id, category_name)
    except StoreError as exc:
        message = str(exc)
        if "category not found" in message.lower():
            message += _suggest_category(store, category_name)
        return {"ok": False, "error": message}
    message = (
        f"Categorized {transaction_id} as {category_name}. "
        "The override beats any rule and applies immediately to all reads."
    )
    if was_pending:
        message += (
            " Note: this transaction is still pending — if the bank issues a "
            "new id when it settles, this override will not carry over (it is "
            "cleaned up with the stale pending row). add_category_rule is "
            "durable across settlement."
        )
    return {"ok": True, "message": message}


def uncategorize_transaction(store: FinanceStore, transaction_id: str) -> dict[str, Any]:
    """Clear a manual override. Idempotent."""
    try:
        store.clear_transaction_override(transaction_id)
    except StoreError as exc:
        return {"ok": False, "error": str(exc)}
    return {
        "ok": True,
        "message": f"Cleared override for {transaction_id}. The transaction now "
        "resolves through rules (or 'Uncategorized').",
    }


def add_category_rule(
    store: FinanceStore,
    category_name: str,
    match_type: str,
    pattern: str,
    priority: int = 100,
    min_amount: Decimal | None = None,
    max_amount: Decimal | None = None,
) -> dict[str, Any]:
    """Add a categorization rule. Retroactive — applies to all existing
    transactions through the read-time view. Optional amount bounds
    refine the pattern match by abs(amount), half-open [min, max)."""
    try:
        normalized_match = parse_match_type(match_type)
        validate_rule_pattern(pattern, normalized_match)
        validate_rule_amount_bounds(min_amount, max_amount)
    except RulePatternError as exc:
        return {"ok": False, "error": f"rule validation failed: {exc}"}
    try:
        rule_id = store.add_rule(
            category_name,
            match_type=normalized_match,
            pattern=pattern,
            priority=priority,
            min_amount=min_amount,
            max_amount=max_amount,
        )
    except StoreError as exc:
        message = str(exc)
        if "category not found" in message.lower():
            message += _suggest_category(store, category_name)
        return {"ok": False, "error": message}
    bounds = format_rule_bounds(min_amount, max_amount)
    bounds_clause = (
        f" Amount bounds: {bounds} (compared against the absolute amount, "
        "so refunds match too; the max bound is exclusive)."
        if bounds
        else ""
    )
    return {
        "ok": True,
        "rule_id": rule_id,
        "message": f"Added rule {rule_id}: {category_name} {normalized_match} "
        f"{pattern!r} (priority {priority}). Applies retroactively to every "
        f"matching transaction.{bounds_clause}",
    }


def remove_category_rule(store: FinanceStore, rule_id: int) -> dict[str, Any]:
    """Remove a user-added categorization rule. Retroactive — transactions
    it matched immediately resolve through the remaining rules.

    Default (seeded) rules are refused: the MCP surface deliberately has
    no ``force`` parameter, because a prompt-injected call could pass it
    as easily as it could call the tool. The CLI's ``remove-rule --force``
    path exists for defaults, with its typed-pattern confirmation.
    """
    try:
        store.remove_rule(rule_id, force=False)
    except StoreError as exc:
        message = str(exc)
        if "default rule" in message:
            message = (
                f"rule {rule_id} is a default (seeded) rule; this tool refuses "
                "to remove those. The user can remove it via the CLI, which "
                "requires a typed confirmation: "
                f"goetta-finance category remove-rule {rule_id} --force"
            )
        return {"ok": False, "error": message}
    return {
        "ok": True,
        "message": f"Removed rule {rule_id}. Transactions it matched now "
        "resolve through the remaining rules (or 'Uncategorized') — "
        "retroactive, no backfill.",
    }
