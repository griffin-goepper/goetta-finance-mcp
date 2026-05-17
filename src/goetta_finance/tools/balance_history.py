from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from goetta_finance.store import FinanceStore


def account_balance_history(
    store: FinanceStore, account_id: str, *, days: int = 90
) -> list[dict[str, Any]]:
    since = datetime.now(tz=UTC) - timedelta(days=days)
    snapshots = store.get_balance_history(account_id=account_id, since=since)
    return [{"timestamp": s.timestamp.isoformat(), "balance": str(s.balance)} for s in snapshots]
