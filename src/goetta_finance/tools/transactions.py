from __future__ import annotations

from datetime import datetime
from typing import Any

from goetta_finance.models import Transaction
from goetta_finance.store import FinanceStore


def serialize_transaction(t: Transaction) -> dict[str, Any]:
    """Pydantic Transaction → JSON-friendly dict. Used by callers that
    still consume ``store.get_transactions`` directly (the web dashboard).
    The MCP ``get_transactions`` tool uses ``_serialize_row_with_category``
    instead so the resolved category is always present."""
    return {
        "id": t.id,
        "account_id": t.account_id,
        "posted": t.posted.isoformat(),
        "transacted_at": t.transacted_at.isoformat() if t.transacted_at else None,
        "amount": str(t.amount),
        "description": t.description,
        "payee": t.payee,
        "memo": t.memo,
    }


def _serialize_row_with_category(row: dict[str, Any]) -> dict[str, Any]:
    """Row from ``store.get_transactions_with_category`` → MCP-tool dict.

    Same key set as ``serialize_transaction`` PLUS ``category`` and
    ``category_color``. Category is never ``None`` — the view falls back
    to literal ``"Uncategorized"`` so Claude always gets a string.
    """
    posted = row["posted"]
    transacted_at = row.get("transacted_at")
    return {
        "id": row["id"],
        "account_id": row["account_id"],
        "posted": posted.isoformat() if isinstance(posted, datetime) else str(posted),
        "transacted_at": (
            transacted_at.isoformat() if isinstance(transacted_at, datetime) else None
        ),
        "amount": str(row["amount"]),
        "description": row["description"],
        "payee": row.get("payee"),
        "memo": row.get("memo"),
        "category": row["category"],
        "category_color": row.get("category_color"),
    }


def get_transactions(
    store: FinanceStore,
    *,
    account_id: str | None = None,
    start: datetime | None = None,
    end: datetime | None = None,
    category: str | None = None,
    include_hidden: bool = False,
    search: str | None = None,
    limit: int = 100,
) -> list[dict[str, Any]]:
    """MCP-tool wrapper. Always routes through the
    ``transactions_with_category`` view so every returned dict carries a
    resolved ``category`` field. Transactions from hidden accounts are
    filtered by default (``include_hidden=True`` opts back in)."""
    rows = store.get_transactions_with_category(
        account_id=account_id,
        start=start,
        end=end,
        category=category,
        include_hidden=include_hidden,
        limit=limit,
    )
    if search:
        needle = search.lower()
        rows = [
            r
            for r in rows
            if needle in r["description"].lower()
            or (r.get("payee") is not None and needle in r["payee"].lower())
        ]
    return [_serialize_row_with_category(r) for r in rows]
