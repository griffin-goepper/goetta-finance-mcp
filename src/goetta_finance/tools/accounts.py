from __future__ import annotations

from typing import Any

from goetta_finance.models import Account
from goetta_finance.store import FinanceStore


def serialize_account(a: Account) -> dict[str, Any]:
    return {
        "id": a.id,
        "name": a.name,
        "org_name": a.org_name,
        "currency": a.currency,
        "balance": str(a.balance),
        "available_balance": (
            str(a.available_balance) if a.available_balance is not None else None
        ),
        "balance_date": a.balance_date.isoformat(),
        "type": a.type.value if a.type is not None else None,
        "is_manual": a.is_manual,
        "is_liability": a.is_liability,
    }


def list_accounts(store: FinanceStore) -> list[dict[str, Any]]:
    return [serialize_account(a) for a in store.get_accounts()]
