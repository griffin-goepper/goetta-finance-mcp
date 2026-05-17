from __future__ import annotations

from typing import Any

from goetta_finance.collector import collect, collect_lock
from goetta_finance.simplefin import SimpleFinClient
from goetta_finance.store import FinanceStore


def sync_now(store: FinanceStore, client: SimpleFinClient | None) -> dict[str, Any]:
    if client is None:
        return {
            "ok": False,
            "error": ("No SimpleFIN access URL configured. Run `goetta-finance init`."),
        }
    if not collect_lock.acquire(blocking=False):
        return {
            "ok": True,
            "skipped": True,
            "reason": "A sync is already running; this call did nothing.",
        }
    try:
        run = collect(store, client)
    finally:
        collect_lock.release()
    return {
        "ok": True,
        "transactions_new": run.transactions_new,
        "transactions_updated": run.transactions_updated,
        "accounts_touched": run.accounts_touched,
        "started_at": run.started_at.isoformat(),
        "finished_at": run.finished_at.isoformat() if run.finished_at else None,
        "warnings": run.warnings,
        "errors": run.errors,
    }
