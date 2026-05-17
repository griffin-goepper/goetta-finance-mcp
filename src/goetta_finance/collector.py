from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

from goetta_finance.models import BalanceSnapshot, SyncRun
from goetta_finance.simplefin import (
    SimpleFinClient,
    parse_accounts,
    parse_transactions,
)
from goetta_finance.store import FinanceStore

logger = logging.getLogger(__name__)

INITIAL_LOOKBACK_DAYS = 90


def _now_utc() -> datetime:
    return datetime.now(tz=UTC)


def collect(
    store: FinanceStore,
    client: SimpleFinClient,
    *,
    overlap_days: int = 5,
    now: datetime | None = None,
) -> SyncRun:
    """Pull from SimpleFIN and write to the store.

    Re-pulls the last ``overlap_days`` of data on every run because banks
    post transactions late. On first run (no prior sync), pulls
    ``INITIAL_LOOKBACK_DAYS`` of history.
    """
    end = now or _now_utc()
    last = store.last_sync_time()
    if last is None:
        start = end - timedelta(days=INITIAL_LOOKBACK_DAYS)
    else:
        start = last - timedelta(days=overlap_days)

    run = SyncRun(started_at=end)
    touched: set[str] = set()

    try:
        for chunk in client.fetch_chunked(start, end):
            for warning in chunk.get("errors") or []:
                run.warnings.append(str(warning))

            accounts = parse_accounts(chunk)
            txns = parse_transactions(chunk)

            if accounts:
                store.upsert_accounts(accounts)
            if txns:
                result = store.upsert_transactions(txns)
                run.transactions_new += result.new
                run.transactions_updated += result.updated

            for acct in accounts:
                touched.add(acct.id)
                store.record_balance_snapshot(
                    BalanceSnapshot(
                        account_id=acct.id,
                        balance=acct.balance,
                        timestamp=acct.balance_date,
                    )
                )
    except Exception as exc:
        run.errors.append(f"{type(exc).__name__}: {exc}")
        run.accounts_touched = len(touched)
        run.finished_at = _now_utc()
        store.record_sync_run(run)
        raise

    run.accounts_touched = len(touched)
    run.finished_at = _now_utc()
    store.record_sync_run(run)
    return run
