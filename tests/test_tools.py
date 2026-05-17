from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from goetta_finance.models import Account, AccountType, BalanceSnapshot, Transaction
from goetta_finance.store.duckdb_store import DuckDBStore
from goetta_finance.tools.accounts import list_accounts
from goetta_finance.tools.balance_history import account_balance_history
from goetta_finance.tools.sql_query import sql_query
from goetta_finance.tools.sync_now import sync_now
from goetta_finance.tools.transactions import get_transactions


def _seed(store: DuckDBStore) -> None:
    accounts = [
        Account(
            id="a1",
            org_name="Chase",
            name="Checking",
            balance=Decimal("100.00"),
            available_balance=Decimal("100.00"),
            balance_date=datetime(2026, 5, 1, tzinfo=UTC),
            type=AccountType.CHECKING,
        ),
        Account(
            id="a2",
            org_name="Vanguard",
            name="Brokerage",
            balance=Decimal("50000.00"),
            balance_date=datetime(2026, 5, 1, tzinfo=UTC),
            type=AccountType.INVESTMENT,
        ),
    ]
    store.upsert_accounts(accounts)
    txns = [
        Transaction(
            id="t1",
            account_id="a1",
            posted=datetime(2026, 4, 15, tzinfo=UTC),
            amount=Decimal("-12.50"),
            description="Starbucks Coffee",
            payee="Starbucks",
        ),
        Transaction(
            id="t2",
            account_id="a1",
            posted=datetime(2026, 5, 1, tzinfo=UTC),
            amount=Decimal("-1200.00"),
            description="Rent payment",
            payee="Landlord",
        ),
        Transaction(
            id="t3",
            account_id="a2",
            posted=datetime(2026, 5, 10, tzinfo=UTC),
            amount=Decimal("500.00"),
            description="Dividend",
            payee="VTSAX",
        ),
    ]
    store.upsert_transactions(txns)
    for i in range(5):
        ts = datetime(2026, 5, 1, tzinfo=UTC) - timedelta(days=i)
        store.record_balance_snapshot(
            BalanceSnapshot(account_id="a1", timestamp=ts, balance=Decimal(f"{100 + i}.00"))
        )


def test_list_accounts_serializes_decimal_and_datetime(
    store: DuckDBStore,
) -> None:
    _seed(store)
    result = list_accounts(store)
    assert len(result) == 2
    chk = next(r for r in result if r["id"] == "a1")
    assert chk["balance"] == "100.00"
    assert chk["balance_date"].startswith("2026-05-01")
    assert chk["type"] == "checking"


def test_get_transactions_filters_and_search(store: DuckDBStore) -> None:
    _seed(store)
    all_txns = get_transactions(store)
    assert {t["id"] for t in all_txns} == {"t1", "t2", "t3"}

    by_account = get_transactions(store, account_id="a1")
    assert {t["id"] for t in by_account} == {"t1", "t2"}

    searched = get_transactions(store, search="rent")
    assert {t["id"] for t in searched} == {"t2"}

    payee_match = get_transactions(store, search="starbucks")
    assert {t["id"] for t in payee_match} == {"t1"}


def test_get_transactions_amount_is_string(store: DuckDBStore) -> None:
    _seed(store)
    txn = get_transactions(store, account_id="a1", limit=1)[0]
    assert isinstance(txn["amount"], str)


@pytest.mark.parametrize("days,expected", [(365, 5), (1, 1)])
def test_account_balance_history_respects_days(
    store: DuckDBStore,
    days: int,
    expected: int,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _seed(store)
    # Freeze "now" so the lookback is deterministic relative to the seed.
    import goetta_finance.tools.balance_history as bh

    fixed_now = datetime(2026, 5, 1, 12, tzinfo=UTC)

    class _FakeDatetime:
        @staticmethod
        def now(tz: object | None = None) -> datetime:
            return fixed_now

    monkeypatch.setattr(bh, "datetime", _FakeDatetime)
    result = account_balance_history(store, "a1", days=days)
    assert len(result) == expected


def test_sql_query_serializes_decimal(store: DuckDBStore) -> None:
    _seed(store)
    result = sql_query(store, "SELECT id, balance FROM accounts ORDER BY id")
    assert result == [
        {"id": "a1", "balance": "100.00"},
        {"id": "a2", "balance": "50000.00"},
    ]


def test_sync_now_without_client_returns_error_payload(
    store: DuckDBStore,
) -> None:
    result = sync_now(store, client=None)
    assert result["ok"] is False
    assert "init" in result["error"].lower()
