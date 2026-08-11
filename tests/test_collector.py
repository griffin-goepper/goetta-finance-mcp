from __future__ import annotations

import copy
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest

from goetta_finance.collector import INITIAL_LOOKBACK_DAYS, collect
from goetta_finance.errors import SimpleFinError
from goetta_finance.models import Account, AccountType, BalanceSnapshot
from goetta_finance.simplefin import SimpleFinClient
from goetta_finance.store.duckdb_store import DuckDBStore


class StubClient(SimpleFinClient):
    """Records fetch windows and replays a static response per call."""

    def __init__(self, response: dict[str, Any]) -> None:
        self.response = response
        self.windows: list[tuple[datetime, datetime]] = []

    def fetch(  # type: ignore[override]
        self, start: datetime, end: datetime
    ) -> dict[str, Any]:
        self.windows.append((start, end))
        return copy.deepcopy(self.response)


def _count(store: DuckDBStore, table: str) -> int:
    row = store.conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()
    assert row is not None
    return int(row[0])


def test_first_run_uses_initial_lookback_window(store: DuckDBStore, demo_response: dict) -> None:
    client = StubClient(demo_response)
    now = datetime(2026, 5, 23, tzinfo=UTC)
    collect(store, client, now=now)

    assert len(client.windows) >= 1
    first_start, _ = client.windows[0]
    expected_start = now - timedelta(days=INITIAL_LOOKBACK_DAYS)
    assert first_start == expected_start


def test_first_run_records_data(store: DuckDBStore, demo_response: dict) -> None:
    client = StubClient(demo_response)
    now = datetime(2026, 5, 23, tzinfo=UTC)
    run = collect(store, client, now=now)

    assert _count(store, "accounts") == 2
    assert _count(store, "transactions") == 5  # includes the 2 pending rows
    assert _count(store, "balance_snapshots") == 2
    # The 90-day initial lookback splits into two chunks; the stub replays
    # the same data on both, so chunk 2 sees the rows as "updated".
    # What matters: every unique row was counted "new" exactly once.
    assert run.transactions_new == 5
    assert run.accounts_touched == 2
    assert run.finished_at is not None
    assert run.errors == []


def test_second_run_is_idempotent(store: DuckDBStore, demo_response: dict) -> None:
    client = StubClient(demo_response)
    now1 = datetime(2026, 5, 23, tzinfo=UTC)
    collect(store, client, now=now1)
    now2 = now1 + timedelta(hours=1)
    run2 = collect(store, client, now=now2)

    # Row counts unchanged: balance_date in the fixture is constant,
    # so the snapshot PK dedups. The pending rows survive because their
    # ids are still in the feed (reconcile keep-set).
    assert _count(store, "accounts") == 2
    assert _count(store, "transactions") == 5
    assert _count(store, "balance_snapshots") == 2
    assert run2.transactions_new == 0
    assert run2.transactions_updated == 5


def test_subsequent_run_uses_overlap_window(store: DuckDBStore, demo_response: dict) -> None:
    client = StubClient(demo_response)
    now1 = datetime(2026, 5, 23, tzinfo=UTC)
    collect(store, client, now=now1)

    # Snapshot last_sync_time BEFORE the second run — that's the value
    # the second collect's overlap calc reads. (Reading it after the
    # second run gives the new run's finished_at, not the previous.)
    prior_last_sync = store.last_sync_time()
    assert prior_last_sync is not None

    now2 = now1 + timedelta(days=1)
    client.windows.clear()
    collect(store, client, now=now2, overlap_days=5)
    assert client.windows, "expected at least one fetch on the second run"

    expected_start = prior_last_sync - timedelta(days=5)
    first_start, _ = client.windows[0]
    assert first_start == expected_start


def test_snapshot_grows_when_balance_date_advances(store: DuckDBStore, demo_response: dict) -> None:
    client = StubClient(demo_response)
    collect(store, client, now=datetime(2026, 5, 23, tzinfo=UTC))
    assert _count(store, "balance_snapshots") == 2

    later = copy.deepcopy(demo_response)
    for a in later["accounts"]:
        a["balance-date"] = a["balance-date"] + 86400  # +1 day
    client.response = later

    collect(store, client, now=datetime(2026, 5, 24, tzinfo=UTC))
    assert _count(store, "balance_snapshots") == 4


def test_passes_through_simplefin_warnings(store: DuckDBStore, demo_response: dict) -> None:
    warning_response = copy.deepcopy(demo_response)
    warning_response["errors"] = ["Bank XYZ only returned 30 days"]
    client = StubClient(warning_response)
    run = collect(store, client, now=datetime(2026, 5, 23, tzinfo=UTC))
    assert "Bank XYZ only returned 30 days" in run.warnings


def test_collect_does_not_touch_manual_accounts(store: DuckDBStore, demo_response: dict) -> None:
    """Pin the isolation outcome: a sync leaves manual accounts byte-for-byte unchanged.

    Seeds a manual account with a balance and a snapshot. Runs collect()
    against demo SimpleFIN data. Asserts the manual account's name, balance,
    balance_date, and is_manual flag are identical afterward, and that the
    snapshot count for that account hasn't grown (collect() must not record
    snapshots on manual accounts since it wasn't the one to observe the
    balance).
    """
    manual = Account(
        id="MANUAL-isolation-test",
        org_id=None,
        org_name="Apple",
        name="Apple Savings",
        currency="USD",
        balance=Decimal("30000.00"),
        available_balance=None,
        balance_date=datetime(2026, 5, 17, tzinfo=UTC),
        type=AccountType.SAVINGS,
        extra={},
        is_manual=True,
    )
    store.upsert_accounts([manual])
    store.record_balance_snapshot(
        BalanceSnapshot(
            account_id="MANUAL-isolation-test",
            balance=Decimal("30000.00"),
            timestamp=datetime(2026, 5, 17, tzinfo=UTC),
        )
    )

    client = StubClient(demo_response)
    collect(store, client, now=datetime(2026, 5, 23, tzinfo=UTC))

    accounts = {a.id: a for a in store.get_accounts()}
    assert "MANUAL-isolation-test" in accounts
    survived = accounts["MANUAL-isolation-test"]
    assert survived.is_manual is True
    assert survived.balance == Decimal("30000.00")
    assert survived.name == "Apple Savings"
    assert survived.balance_date == datetime(2026, 5, 17, tzinfo=UTC)

    snap_count = store.conn.execute(
        "SELECT COUNT(*) FROM balance_snapshots WHERE account_id = ?",
        ["MANUAL-isolation-test"],
    ).fetchone()
    assert snap_count is not None and snap_count[0] == 1, (
        "collect() must not record balance_snapshots for manual accounts"
    )


# --- Pending snapshot reconciliation ---------------------------------------------


def _txn_ids(store: DuckDBStore) -> set[str]:
    return {row[0] for row in store.conn.execute("SELECT id FROM transactions").fetchall()}


class SequenceStubClient(SimpleFinClient):
    """Replays one response per fetch call, in order (one per chunk window)."""

    def __init__(self, responses: list[dict[str, Any]]) -> None:
        self.responses = list(responses)
        self.windows: list[tuple[datetime, datetime]] = []

    def fetch(  # type: ignore[override]
        self, start: datetime, end: datetime
    ) -> dict[str, Any]:
        self.windows.append((start, end))
        return copy.deepcopy(self.responses.pop(0))


def test_pending_settled_under_new_id_is_reconciled(
    store: DuckDBStore, demo_response: dict
) -> None:
    """Lifecycle (a): the bank reissues the id at settlement. The stale
    pending row must go; the posted row arrives as a fresh insert."""
    client = StubClient(demo_response)
    now1 = datetime(2026, 5, 23, tzinfo=UTC)
    collect(store, client, now=now1)
    assert "TX-3-PENDING" in _txn_ids(store)

    settled = copy.deepcopy(demo_response)
    chk = next(a for a in settled["accounts"] if a["id"] == "ACT-CHK-1")
    chk["transactions"] = [t for t in chk["transactions"] if t["id"] != "TX-3-PENDING"]
    chk["transactions"].append(
        {
            "id": "TX-3-SETTLED",
            "posted": 1748016000,
            "amount": "-15.99",
            "description": "Pending hold",
            "pending": False,
        }
    )
    client.response = settled
    collect(store, client, now=now1 + timedelta(hours=6))

    ids = _txn_ids(store)
    assert "TX-3-PENDING" not in ids
    assert "TX-3-SETTLED" in ids
    assert "TX-4-PENDING-UNPOSTED" in ids  # still pending, still in the feed


def test_pending_hold_evaporation_is_reconciled(store: DuckDBStore, demo_response: dict) -> None:
    """Lifecycle (c): an auth hold released without posting vanishes from
    the feed and must vanish from the store."""
    client = StubClient(demo_response)
    now1 = datetime(2026, 5, 23, tzinfo=UTC)
    collect(store, client, now=now1)

    released = copy.deepcopy(demo_response)
    chk = next(a for a in released["accounts"] if a["id"] == "ACT-CHK-1")
    chk["transactions"] = [t for t in chk["transactions"] if t["id"] != "TX-3-PENDING"]
    client.response = released
    collect(store, client, now=now1 + timedelta(hours=6))

    ids = _txn_ids(store)
    assert "TX-3-PENDING" not in ids
    assert ids == {"TX-1", "TX-2", "TX-4-PENDING-UNPOSTED", "TX-VG-1"}


def test_reconcile_unions_ids_across_chunks(store: DuckDBStore, demo_response: dict) -> None:
    """A multi-chunk sync sees pending rows only in the recent window.
    Reconciling per-chunk would delete them while processing the older
    window; the reconcile must run once over the union of all chunks."""
    client = StubClient(demo_response)
    now1 = datetime(2026, 5, 23, tzinfo=UTC)
    collect(store, client, now=now1)

    old_window = copy.deepcopy(demo_response)
    for a in old_window["accounts"]:
        a["transactions"] = []
    seq = SequenceStubClient([old_window, demo_response])
    collect(store, seq, now=now1 + timedelta(days=90))

    assert len(seq.windows) == 2, "expected the sync to span two chunk windows"
    ids = _txn_ids(store)
    assert "TX-3-PENDING" in ids
    assert "TX-4-PENDING-UNPOSTED" in ids


def test_account_with_null_transactions_is_not_reconciled(
    store: DuckDBStore, demo_response: dict
) -> None:
    """An institution hiccup (account present, ``transactions: null``)
    must not wipe that account's pending rows against an empty feed."""
    client = StubClient(demo_response)
    now1 = datetime(2026, 5, 23, tzinfo=UTC)
    collect(store, client, now=now1)

    outage = copy.deepcopy(demo_response)
    next(a for a in outage["accounts"] if a["id"] == "ACT-CHK-1")["transactions"] = None
    client.response = outage
    collect(store, client, now=now1 + timedelta(hours=6))

    ids = _txn_ids(store)
    assert "TX-3-PENDING" in ids
    assert "TX-4-PENDING-UNPOSTED" in ids


def test_failed_sync_does_not_reconcile(store: DuckDBStore, demo_response: dict) -> None:
    """A sync that dies mid-chunks has a partial view of the feed and must
    not delete pending rows based on it."""
    client = StubClient(demo_response)
    now1 = datetime(2026, 5, 23, tzinfo=UTC)
    collect(store, client, now=now1)

    empty = copy.deepcopy(demo_response)
    for a in empty["accounts"]:
        a["transactions"] = []

    class FailsOnSecondChunk(SimpleFinClient):
        def __init__(self) -> None:
            self.calls = 0

        def fetch(  # type: ignore[override]
            self, start: datetime, end: datetime
        ) -> dict[str, Any]:
            self.calls += 1
            if self.calls >= 2:
                raise SimpleFinError("bridge fell over mid-sync")
            return copy.deepcopy(empty)

    with pytest.raises(SimpleFinError):
        collect(store, FailsOnSecondChunk(), now=now1 + timedelta(days=90))

    ids = _txn_ids(store)
    assert "TX-3-PENDING" in ids
    assert "TX-4-PENDING-UNPOSTED" in ids
