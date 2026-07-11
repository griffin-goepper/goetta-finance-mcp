"""Transfer-link MCP tools (migration 0012).

Write tools return structured ``{ok: bool, ...}`` dicts and never raise —
the ``set_goal``/``add_category_rule`` contract. The pattern write surface
is gated by the same shared validator as the CLI
(``validators.validate_rule_pattern``); the roll-forward and suggestion
math lives in ``transfers.py`` (one home) — these wrappers only shape
JSON. Money is emitted as strings, timestamps as ISO 8601, per the
``_serialize`` conventions.
"""

from __future__ import annotations

from typing import Any

from goetta_finance.errors import StoreError
from goetta_finance.models import TransferLink, TransferLinkSuggestion
from goetta_finance.store import FinanceStore
from goetta_finance.transfers import apply_transfer_links, transfer_link_suggestions
from goetta_finance.validators import (
    RulePatternError,
    parse_match_type,
    validate_rule_pattern,
)


def serialize_link(link: TransferLink) -> dict[str, Any]:
    return {
        "id": link.id,
        "account_id": link.account_id,
        "account_name": link.account_name,
        "source_account_id": link.source_account_id,
        "source_account_name": link.source_account_name,
        "match_type": link.match_type,
        "pattern": link.pattern,
        "anchor": link.anchor.isoformat(),
        "created_at": link.created_at.isoformat(),
    }


def serialize_suggestion(suggestion: TransferLinkSuggestion) -> dict[str, Any]:
    return {
        "account_id": suggestion.account_id,
        "account_name": suggestion.account_name,
        "source_account_id": suggestion.source_account_id,
        "source_account_name": suggestion.source_account_name,
        "payee": suggestion.payee,
        "transaction_count": suggestion.transaction_count,
        "total": str(suggestion.total),
        "first_posted": suggestion.first_posted.isoformat(),
        "last_posted": suggestion.last_posted.isoformat(),
        "pending_count": suggestion.pending_count,
        "pending_total": str(suggestion.pending_total),
        "suggested_command": suggestion.suggested_command,
    }


def list_transfer_links(store: FinanceStore) -> dict[str, Any]:
    """Existing links plus detected candidates for linkless manual accounts."""
    return {
        "links": [serialize_link(link) for link in store.list_transfer_links()],
        "suggestions": [serialize_suggestion(s) for s in transfer_link_suggestions(store)],
    }


def link_account_transfers(
    store: FinanceStore,
    *,
    account_id: str,
    source_account_id: str,
    pattern: str,
    match_type: str = "contains",
) -> dict[str, Any]:
    """Create a transfer link and immediately roll forward eligible transfers."""
    try:
        normalized = parse_match_type(match_type)
        validate_rule_pattern(pattern, normalized)
    except RulePatternError as exc:
        return {"ok": False, "error": f"link validation failed: {exc}"}
    try:
        link = store.add_transfer_link(
            account_id, source_account_id, match_type=normalized, pattern=pattern
        )
        applied = apply_transfer_links(store)
    except StoreError as exc:
        return {"ok": False, "error": str(exc)}
    return {
        "ok": True,
        "link_id": link.id,
        "applied": applied,
        "message": (
            f"Linked {link.account_name or link.account_id} to matching transfers on "
            f"{link.source_account_name or link.source_account_id}. Matched transactions "
            "posted after the account's balance date were applied now; each sync rolls "
            "new ones forward automatically. `goetta-finance account set-balance` stays "
            "the true-up for interest and anything the pattern misses."
        ),
    }


def unlink_account_transfers(store: FinanceStore, link_id: int) -> dict[str, Any]:
    """Delete a transfer link by id."""
    try:
        store.remove_transfer_link(link_id)
    except StoreError as exc:
        return {"ok": False, "error": str(exc)}
    return {
        "ok": True,
        "message": (
            f"Removed transfer link {link_id}. Already-applied transfers stay in the "
            "balance, and the application ledger prevents double-counting on re-link."
        ),
    }
