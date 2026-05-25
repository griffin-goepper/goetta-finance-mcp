from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from goetta_finance.models import Account, AccountType, BalanceSnapshot, Transaction
from goetta_finance.store.duckdb_store import DuckDBStore
from goetta_finance.web.charts import (
    net_worth_figure,
    spending_by_category_figure,
    spending_figure,
)


def _seed(store: DuckDBStore) -> None:
    store.upsert_accounts(
        [
            Account(
                id="a1",
                org_name="Chase",
                name="Checking",
                balance=Decimal("100.00"),
                balance_date=datetime(2026, 5, 1, tzinfo=UTC),
                type=AccountType.CHECKING,
            )
        ]
    )
    store.record_balance_snapshot(
        BalanceSnapshot(
            account_id="a1",
            timestamp=datetime(2026, 5, 1, tzinfo=UTC),
            balance=Decimal("100.00"),
        )
    )
    store.record_balance_snapshot(
        BalanceSnapshot(
            account_id="a1",
            timestamp=datetime(2026, 5, 2, tzinfo=UTC),
            balance=Decimal("150.00"),
        )
    )
    store.upsert_transactions(
        [
            Transaction(
                id="t1",
                account_id="a1",
                posted=datetime(2026, 5, 5, tzinfo=UTC),
                amount=Decimal("1000.00"),
                description="paycheck",
            ),
            Transaction(
                id="t2",
                account_id="a1",
                posted=datetime(2026, 5, 6, tzinfo=UTC),
                amount=Decimal("-200.00"),
                description="rent",
            ),
        ]
    )


def test_net_worth_figure_shape(store: DuckDBStore) -> None:
    _seed(store)
    fig = net_worth_figure(store, days=30, now=datetime(2026, 5, 16, tzinfo=UTC))
    assert set(fig.keys()) == {"data", "layout"}
    assert len(fig["data"]) == 1
    trace = fig["data"][0]
    assert trace["type"] == "scatter"
    assert len(trace["x"]) == 2  # two snapshot days
    assert list(trace["y"]) == [100.0, 150.0]


def test_spending_figure_has_income_and_spending_traces(store: DuckDBStore) -> None:
    _seed(store)
    fig = spending_figure(store, months=3, now=datetime(2026, 5, 16, tzinfo=UTC))
    assert set(fig.keys()) == {"data", "layout"}
    assert len(fig["data"]) == 2
    names = {t["name"] for t in fig["data"]}
    assert names == {"Income", "Spending"}
    income_trace = next(t for t in fig["data"] if t["name"] == "Income")
    spending_trace = next(t for t in fig["data"] if t["name"] == "Spending")
    # Spending plotted as negative for stacked-below-zero effect
    assert min(spending_trace["y"]) < 0
    assert max(income_trace["y"]) > 0


def test_spending_figure_handles_empty_store(store: DuckDBStore) -> None:
    fig = spending_figure(store, months=3, now=datetime(2026, 5, 16, tzinfo=UTC))
    assert set(fig.keys()) == {"data", "layout"}
    assert len(fig["data"]) == 2
    for trace in fig["data"]:
        assert all(v == 0 for v in trace["y"])


def test_net_worth_figure_handles_empty_store(store: DuckDBStore) -> None:
    fig = net_worth_figure(store, days=30, now=datetime(2026, 5, 16, tzinfo=UTC))
    assert set(fig.keys()) == {"data", "layout"}
    trace = fig["data"][0]
    assert len(trace["x"]) == 0


def test_spending_by_category_figure_uses_pie_trace(store: DuckDBStore) -> None:
    """Pie chart is the load-bearing visualization for the new page.
    Pin the trace type so a future refactor that swaps to a different
    chart (e.g. bar) trips the test and forces a deliberate decision.
    Pie trace is verified bundled in plotly-basic.min.js."""
    store.upsert_accounts(
        [
            Account(
                id="pie-a1",
                org_name="Test",
                name="Checking",
                balance=Decimal("100.00"),
                balance_date=datetime(2026, 5, 1, tzinfo=UTC),
                type=AccountType.CHECKING,
            )
        ]
    )
    now = datetime(2026, 5, 16, tzinfo=UTC)
    store.upsert_transactions(
        [
            Transaction(
                id="pie-sbux",
                account_id="pie-a1",
                posted=now - timedelta(days=2),
                amount=Decimal("-12.50"),
                description="STARBUCKS STORE #1",
            ),
        ]
    )
    fig = spending_by_category_figure(store, days=30, now=now)
    assert set(fig.keys()) == {"data", "layout"}
    assert fig["data"][0]["type"] == "pie"
    assert "Dining" in fig["data"][0]["labels"]


def test_spending_by_category_figure_handles_empty_store(
    store: DuckDBStore,
) -> None:
    """No transactions = empty pie, not an error."""
    fig = spending_by_category_figure(store, days=30, now=datetime(2026, 5, 16, tzinfo=UTC))
    assert fig["data"][0]["type"] == "pie"
    assert list(fig["data"][0]["labels"]) == []
    assert list(fig["data"][0]["values"]) == []
