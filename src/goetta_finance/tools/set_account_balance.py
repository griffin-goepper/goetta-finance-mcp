"""MCP surface for the manual-balance true-up (``account set-balance``).

A thin JSON shaper over ``transfers.true_up_manual_balance`` — the CLI
command calls the same function, so the two write surfaces can't drift
(the ``validators``/``set_goal`` precedent). Returns structured
``{ok: bool, ...}`` results and never raises, the write-tool contract
from ``tools/goals.py``. Money is emitted as strings, timestamps as
ISO 8601.

This surface adds only what's wire-specific: resolving an account id OR
case-insensitive name (with a difflib did-you-mean on typos, mirroring
``_suggest_category``), and parsing the optional ISO ``as_of``. The
refusal gates (unknown account, non-manual account, future ``as_of``)
live in the shared function.
"""

from __future__ import annotations

import difflib
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from goetta_finance.errors import BalanceTrueUpError, StoreError
from goetta_finance.models import Account
from goetta_finance.store import FinanceStore
from goetta_finance.tools.accounts import serialize_account
from goetta_finance.transfers import true_up_manual_balance


def _resolve_account(store: FinanceStore, account: str) -> Account | str:
    """Resolve an id-or-name to an Account, or return an error message.

    Exact id match wins; otherwise a case-insensitive name match. Same
    visibility as the CLI command: hidden accounts are not addressable
    (unhide first). Unknown names get a difflib did-you-mean, the
    ``_suggest_category`` pattern.
    """
    accounts = store.get_accounts()
    stripped = account.strip()
    by_id = {a.id: a for a in accounts}
    if stripped in by_id:
        return by_id[stripped]
    named = [a for a in accounts if a.name.lower() == stripped.lower()]
    if len(named) == 1:
        return named[0]
    if len(named) > 1:
        ids = ", ".join(a.id for a in named)
        return f"account name {stripped!r} is ambiguous (matches {ids}); pass the account id"
    names = [a.name for a in accounts]
    matches = difflib.get_close_matches(stripped, names, n=1, cutoff=0.6)
    suggestion = (
        f' Did you mean "{matches[0]}"?'
        if matches
        else " Call list_accounts for valid ids and names."
    )
    return f"account not found: {stripped}.{suggestion}"


def _parse_as_of(as_of: str | None) -> datetime | str:
    """Parse an optional ISO date/datetime into tz-aware UTC, or return
    an error message. Naive inputs are taken as UTC; ``None`` = now."""
    if as_of is None:
        return datetime.now(tz=UTC)
    try:
        parsed = datetime.fromisoformat(as_of.strip())
    except ValueError:
        return (
            f"as_of must be an ISO date or datetime (e.g. '2026-07-01' or "
            f"'2026-07-01T12:00:00Z'), got {as_of!r}"
        )
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def set_account_balance(
    store: FinanceStore,
    *,
    account: str,
    balance: Decimal,
    as_of: str | None = None,
) -> dict[str, Any]:
    """True-up a manual account's balance via the shared write path."""
    resolved = _resolve_account(store, account)
    if isinstance(resolved, str):
        return {"ok": False, "error": resolved}
    parsed_as_of = _parse_as_of(as_of)
    if isinstance(parsed_as_of, str):
        return {"ok": False, "error": parsed_as_of}
    try:
        result = true_up_manual_balance(store, resolved.id, balance, as_of=parsed_as_of)
    except BalanceTrueUpError as exc:
        message = str(exc)
        if "non-manual" in message:
            message += (
                " Synced accounts get their balance from SimpleFIN on every "
                "sync, so a manual true-up would be overwritten; "
                "set_account_balance is for manual (MANUAL-...) accounts only."
            )
        return {"ok": False, "error": message}
    except StoreError as exc:
        return {"ok": False, "error": str(exc)}
    reanchor_note = (
        f" Re-anchored {result.links_reanchored} transfer link(s) at the as-of "
        "moment; matched transfers posted after it re-applied immediately and "
        "future syncs roll new ones forward without double-counting."
        if result.links_reanchored
        else ""
    )
    return {
        "ok": True,
        "account": serialize_account(result.account),
        "snapshot": {
            "account_id": result.snapshot.account_id,
            "balance": str(result.snapshot.balance),
            "timestamp": result.snapshot.timestamp.isoformat(),
        },
        "links_reanchored": result.links_reanchored,
        "transfers_reapplied": result.applied,
        "message": (
            f"Updated {result.account.name} ({result.account.id}) to "
            f"{result.snapshot.balance:.2f} {result.account.currency} as of "
            f"{result.snapshot.timestamp.isoformat()}." + reanchor_note
        ),
    }
