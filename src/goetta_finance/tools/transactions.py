from __future__ import annotations

from datetime import datetime
from typing import Any

from goetta_finance.models import Transaction
from goetta_finance.store import FinanceStore


def serialize_transaction(t: Transaction) -> dict[str, Any]:
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


def get_transactions(
    store: FinanceStore,
    *,
    account_id: str | None = None,
    start: datetime | None = None,
    end: datetime | None = None,
    search: str | None = None,
    limit: int = 100,
) -> list[dict[str, Any]]:
    txns = store.get_transactions(account_id=account_id, start=start, end=end, limit=limit)
    if search:
        needle = search.lower()
        txns = [
            t
            for t in txns
            if needle in t.description.lower()
            or (t.payee is not None and needle in t.payee.lower())
        ]
    return [serialize_transaction(t) for t in txns]
