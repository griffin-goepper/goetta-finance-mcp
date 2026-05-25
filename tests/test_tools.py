from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from goetta_finance.models import Account, AccountType, BalanceSnapshot, Transaction
from goetta_finance.store.duckdb_store import DuckDBStore
from goetta_finance.tools.accounts import list_accounts
from goetta_finance.tools.balance_history import account_balance_history
from goetta_finance.tools.spending_by_category import spending_by_category
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


# --- Sub-seam 3: spending_by_category + get_transactions(category=) -------
#
# Outcome-pinning. The default rule for "Starbucks" → Dining is seeded by
# migration 0004; tests rely on that rather than seeding rules themselves
# to keep the realism close to dogfooding.


def _seed_cat(store: DuckDBStore) -> None:
    """Account + a Dining-resolving spend, a Groceries-resolving spend, and
    a paycheck overridden to Income."""
    store.upsert_accounts(
        [
            Account(
                id="cat-a1",
                org_name="Test",
                name="Checking",
                balance=Decimal("100.00"),
                balance_date=datetime(2026, 5, 1, tzinfo=UTC),
                type=AccountType.CHECKING,
            )
        ]
    )
    store.upsert_transactions(
        [
            Transaction(
                id="t-sbux",
                account_id="cat-a1",
                posted=datetime(2026, 5, 5, tzinfo=UTC),
                amount=Decimal("-12.50"),
                description="STARBUCKS STORE #1",
            ),
            Transaction(
                id="t-kroger",
                account_id="cat-a1",
                posted=datetime(2026, 5, 10, tzinfo=UTC),
                amount=Decimal("-87.45"),
                description="KROGER #999",
            ),
            Transaction(
                id="t-paycheck",
                account_id="cat-a1",
                posted=datetime(2026, 5, 15, tzinfo=UTC),
                amount=Decimal("3000.00"),
                description="GE AEROSPACE PAYROLL",
            ),
        ]
    )
    # Paycheck override → Income.
    store.set_transaction_override("t-paycheck", "Income")


def test_spending_by_category_aggregation(store: DuckDBStore) -> None:
    _seed_cat(store)
    rows = spending_by_category(
        store, datetime(2026, 5, 1, tzinfo=UTC), datetime(2026, 5, 31, tzinfo=UTC)
    )
    by_cat = {r["category"]: r for r in rows}
    assert by_cat["Dining"]["total"] == "12.50"
    assert by_cat["Groceries"]["total"] == "87.45"
    # Sorted descending by total: Groceries (87.45) > Dining (12.50).
    assert [r["category"] for r in rows][:2] == ["Groceries", "Dining"]


def test_spending_by_category_excludes_income_by_default(store: DuckDBStore) -> None:
    _seed_cat(store)
    rows = spending_by_category(
        store, datetime(2026, 5, 1, tzinfo=UTC), datetime(2026, 5, 31, tzinfo=UTC)
    )
    assert "Income" not in {r["category"] for r in rows}


def test_spending_by_category_includes_income_with_opt_in_negative_total(
    store: DuckDBStore,
) -> None:
    """The Income row's total is negative because SUM(-amount) over a
    positive-amount paycheck = negative magnitude (cash in)."""
    _seed_cat(store)
    rows = spending_by_category(
        store,
        datetime(2026, 5, 1, tzinfo=UTC),
        datetime(2026, 5, 31, tzinfo=UTC),
        include_income=True,
    )
    income = next(r for r in rows if r["category"] == "Income")
    assert Decimal(income["total"]) == Decimal("-3000.00")


def test_spending_by_category_excludes_rule_resolved_refund_in_default(
    store: DuckDBStore,
) -> None:
    """A POSITIVE-amount transaction whose category resolves via the
    default RULE for STARBUCKS → Dining must NOT pollute the Dining
    total in default mode. Exercises the matched_rule branch of the
    view (no override row)."""
    store.upsert_accounts(
        [
            Account(
                id="ref-a1",
                org_name="Test",
                name="Checking",
                balance=Decimal("100.00"),
                balance_date=datetime(2026, 5, 1, tzinfo=UTC),
                type=AccountType.CHECKING,
            )
        ]
    )
    store.upsert_transactions(
        [
            Transaction(
                id="ref-spend",
                account_id="ref-a1",
                posted=datetime(2026, 5, 5, tzinfo=UTC),
                amount=Decimal("-12.50"),
                description="STARBUCKS STORE #1",
            ),
            Transaction(
                id="ref-refund",
                account_id="ref-a1",
                posted=datetime(2026, 5, 7, tzinfo=UTC),
                amount=Decimal("4.00"),  # positive — a refund
                description="STARBUCKS STORE #1 REFUND",
            ),
        ]
    )
    rows = spending_by_category(
        store, datetime(2026, 5, 1, tzinfo=UTC), datetime(2026, 5, 31, tzinfo=UTC)
    )
    dining = next(r for r in rows if r["category"] == "Dining")
    assert dining["total"] == "12.50"  # not 8.50 — the refund is excluded
    assert dining["transaction_count"] == 1


def test_spending_by_category_excludes_override_resolved_refund_in_default(
    store: DuckDBStore,
) -> None:
    """Same as above but the refund's category comes from a manual
    OVERRIDE row, not the default rule. Exercises the
    transaction_overrides branch of the view — distinct code path."""
    store.upsert_accounts(
        [
            Account(
                id="ovr-a1",
                org_name="Test",
                name="Checking",
                balance=Decimal("100.00"),
                balance_date=datetime(2026, 5, 1, tzinfo=UTC),
                type=AccountType.CHECKING,
            )
        ]
    )
    store.upsert_transactions(
        [
            Transaction(
                id="ovr-spend",
                account_id="ovr-a1",
                posted=datetime(2026, 5, 5, tzinfo=UTC),
                amount=Decimal("-50.00"),
                description="UNMATCHED DESCRIPTION 1",  # falls to Uncategorized via rule
            ),
            Transaction(
                id="ovr-refund",
                account_id="ovr-a1",
                posted=datetime(2026, 5, 7, tzinfo=UTC),
                amount=Decimal("20.00"),  # positive — refund
                description="UNMATCHED DESCRIPTION 2",
            ),
        ]
    )
    # Both transactions categorized to Dining via override.
    store.set_transaction_override("ovr-spend", "Dining")
    store.set_transaction_override("ovr-refund", "Dining")

    rows = spending_by_category(
        store, datetime(2026, 5, 1, tzinfo=UTC), datetime(2026, 5, 31, tzinfo=UTC)
    )
    dining = next(r for r in rows if r["category"] == "Dining")
    assert dining["total"] == "50.00"  # not 30.00 — refund excluded
    assert dining["transaction_count"] == 1


def test_spending_by_category_total_is_string(store: DuckDBStore) -> None:
    """Decimal → str serialization, matching the tool conventions."""
    _seed_cat(store)
    rows = spending_by_category(
        store, datetime(2026, 5, 1, tzinfo=UTC), datetime(2026, 5, 31, tzinfo=UTC)
    )
    assert all(isinstance(r["total"], str) for r in rows)


def test_get_transactions_serializes_category_field(store: DuckDBStore) -> None:
    """Every row from the MCP tool carries a non-null `category` key.
    Even for unmatchable descriptions the view falls back to literal
    'Uncategorized' — Claude never sees None."""
    store.upsert_accounts(
        [
            Account(
                id="c-a1",
                org_name="Test",
                name="Checking",
                balance=Decimal("100.00"),
                balance_date=datetime(2026, 5, 1, tzinfo=UTC),
                type=AccountType.CHECKING,
            )
        ]
    )
    store.upsert_transactions(
        [
            Transaction(
                id="c-sbux",
                account_id="c-a1",
                posted=datetime(2026, 5, 5, tzinfo=UTC),
                amount=Decimal("-12.50"),
                description="STARBUCKS STORE #1",
            ),
            Transaction(
                id="c-unmatched",
                account_id="c-a1",
                posted=datetime(2026, 5, 6, tzinfo=UTC),
                amount=Decimal("-50.00"),
                description="ZZZ UNMATCHED PAYEE",
            ),
        ]
    )
    rows = get_transactions(store)
    by_id = {r["id"]: r for r in rows}
    assert by_id["c-sbux"]["category"] == "Dining"
    assert by_id["c-unmatched"]["category"] == "Uncategorized"


def test_get_transactions_category_filter_via_tool(store: DuckDBStore) -> None:
    _seed_cat(store)
    dining_only = get_transactions(store, category="Dining")
    assert [r["id"] for r in dining_only] == ["t-sbux"]
    assert dining_only[0]["category"] == "Dining"


def test_get_transactions_search_still_works_with_category(
    store: DuckDBStore,
) -> None:
    """The in-Python search filter must keep working after the dict
    refactor (the tool now reads description/payee from dict keys, not
    Transaction attributes)."""
    _seed_cat(store)
    starbucks = get_transactions(store, search="starbucks")
    assert {r["id"] for r in starbucks} == {"t-sbux"}


# --- Perf regression gate for the view-routed get_transactions path -------
#
# Measure-then-pin, same shape as test_view_planner_under_10k_transactions
# in tests/test_duckdb_store.py. Baseline observed during sub-seam-3
# implementation; if this trips on a faster machine, re-measure and
# update _GET_TXNS_MEDIAN_BASELINE_MS below.

_GET_TXNS_MEDIAN_BASELINE_MS = 30.0  # measured 2026-05-21 on dev machine (Windows)
_GET_TXNS_REGRESSION_THRESHOLD_MS = min(5 * _GET_TXNS_MEDIAN_BASELINE_MS, 250.0)


def test_get_transactions_view_route_perf_under_10k(store: DuckDBStore) -> None:
    """Routing every get_transactions call through the view adds the
    matched_rule join cost. Pin a regression threshold so a future
    schema or query change that makes this materially slower fails
    here, not in a Claude conversation."""
    import statistics
    import time

    store.upsert_accounts(
        [
            Account(
                id="perf-a1",
                org_name="Test",
                name="Checking",
                balance=Decimal("100.00"),
                balance_date=datetime(2026, 5, 1, tzinfo=UTC),
                type=AccountType.CHECKING,
            )
        ]
    )
    # Bulk insert in-engine; the executemany cost dominated earlier
    # versions of the sub-seam 1 perf probe (matches that pattern).
    store.conn.execute(
        """
        INSERT INTO transactions
            (id, account_id, posted, transacted_at, amount, description,
             payee, memo, pending, extra)
        SELECT
            printf('gt-perf-%05d', i) AS id,
            'perf-a1' AS account_id,
            TIMESTAMP '2026-05-10 12:00:00' - INTERVAL (i) HOUR AS posted,
            NULL AS transacted_at,
            CAST(-1.00 AS DECIMAL(18,2)) AS amount,
            CASE i % 4
                WHEN 0 THEN 'STARBUCKS #' || i
                WHEN 1 THEN 'KROGER #' || i
                WHEN 2 THEN 'ZZZ UNMATCHED ' || i
                ELSE 'SHELL OIL #' || i
            END AS description,
            NULL AS payee, NULL AS memo, FALSE AS pending, NULL AS extra
        FROM range(0, 10000) AS t(i)
        """
    )

    durations_ms: list[float] = []
    for _ in range(10):
        t0 = time.perf_counter()
        rows = get_transactions(store, limit=100)
        durations_ms.append((time.perf_counter() - t0) * 1000.0)
        assert len(rows) == 100
        assert all("category" in r for r in rows)
    median_ms = statistics.median(durations_ms)
    assert median_ms <= _GET_TXNS_REGRESSION_THRESHOLD_MS, (
        f"get_transactions(limit=100) median {median_ms:.1f}ms exceeds "
        f"regression threshold {_GET_TXNS_REGRESSION_THRESHOLD_MS:.1f}ms "
        f"(5x of measured baseline {_GET_TXNS_MEDIAN_BASELINE_MS:.1f}ms "
        f"or 250ms ceiling). All durations: "
        f"{[round(d, 1) for d in durations_ms]}"
    )
