from __future__ import annotations

import logging
import threading
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

# Process-global single-writer guard. Both the scheduler tick (daemon mode)
# and the lazy-sync MCP hook acquire this before calling ``collect``. If a
# collect is already running, callers should ``acquire(blocking=False)``,
# observe ``False``, and skip silently. DuckDB tolerates one writer at a
# time — this enforces that without spinning a queue.
collect_lock = threading.Lock()


def collect_under_lock(store: FinanceStore, client: SimpleFinClient) -> SyncRun | None:
    """Acquire the collect lock and run ``collect()`` synchronously.

    Returns the ``SyncRun`` if the lock was free and the sync ran. Returns
    ``None`` if another sync is already in progress — the caller should
    not retry, the in-flight sync will land the data.
    """
    if not collect_lock.acquire(blocking=False):
        return None
    try:
        return collect(store, client)
    finally:
        collect_lock.release()


def trigger_background_collect(store: FinanceStore, client: SimpleFinClient) -> bool:
    """Try to start a background ``collect()`` in a daemon OS thread.

    Returns ``True`` if a new sync was started, ``False`` if one is already
    running (or the lock could not be acquired immediately). The thread owns
    the lock for its lifetime and releases it on exit, exception or not.

    Uses a real OS thread, not ``asyncio.create_task``, because the latter
    is bound to the request's event loop and may be cancelled when the
    response returns — silently losing the sync.
    """
    if not collect_lock.acquire(blocking=False):
        logger.debug("background collect skipped: sync already in progress")
        return False

    def _runner() -> None:
        try:
            collect(store, client)
        except Exception:
            logger.exception("background collect failed")
        finally:
            collect_lock.release()

    thread = threading.Thread(
        target=_runner,
        name="goetta-finance-bg-collect",
        daemon=True,
    )
    thread.start()
    return True


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
