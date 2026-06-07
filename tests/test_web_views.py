from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from goetta_finance.models import (
    Account,
    AccountType,
    BalanceSnapshot,
    SyncRun,
    Transaction,
)
from goetta_finance.store.duckdb_store import DuckDBStore
from goetta_finance.web.app import build_app


def _seed(store: DuckDBStore) -> None:
    store.upsert_accounts(
        [
            Account(
                id="acc-checking",
                org_name="Chase",
                name="Checking 1234",
                balance=Decimal("2543.21"),
                available_balance=Decimal("2500.00"),
                balance_date=datetime(2026, 5, 1, tzinfo=UTC),
                type=AccountType.CHECKING,
            ),
            Account(
                id="acc-brokerage",
                org_name="Vanguard",
                name="Brokerage",
                balance=Decimal("50000.00"),
                balance_date=datetime(2026, 5, 1, tzinfo=UTC),
                type=AccountType.INVESTMENT,
            ),
        ]
    )
    # Posted dates relative to "now" so the dashboard's hardcoded
    # "last 30 days" window always includes them. Otherwise these
    # tests rot as the calendar advances past the previously-fresh
    # fixed date.
    recent = datetime.now(tz=UTC) - timedelta(days=3)
    store.upsert_transactions(
        [
            Transaction(
                id="tx-spotify",
                account_id="acc-checking",
                posted=recent,
                amount=Decimal("-9.99"),
                description="Spotify Premium",
                payee="Spotify",
            ),
            Transaction(
                id="tx-paycheck",
                account_id="acc-checking",
                posted=recent + timedelta(days=1),
                amount=Decimal("4500.00"),
                description="Paycheck",
                payee="Acme Corp",
            ),
        ]
    )
    store.record_balance_snapshot(
        BalanceSnapshot(
            account_id="acc-checking",
            timestamp=datetime(2026, 5, 1, tzinfo=UTC),
            balance=Decimal("2543.21"),
        )
    )
    store.record_sync_run(
        SyncRun(
            started_at=datetime(2026, 5, 16, 6, tzinfo=UTC),
            finished_at=datetime(2026, 5, 16, 6, 1, tzinfo=UTC),
            accounts_touched=2,
            transactions_new=2,
            warnings=["Bank XYZ only returned 30 days"],
        )
    )


@pytest.fixture
def client(store: DuckDBStore) -> Iterator[TestClient]:
    _seed(store)
    app = build_app(store)
    with TestClient(app) as c:
        yield c


def test_accounts_page_renders(client: TestClient) -> None:
    resp = client.get("/")
    assert resp.status_code == 200
    body = resp.text
    assert "Checking 1234" in body
    assert "Brokerage" in body
    assert "2543.21" in body


def test_net_worth_page_renders_with_plotly(client: TestClient) -> None:
    resp = client.get("/net-worth?days=30")
    assert resp.status_code == 200
    body = resp.text
    assert "Plotly.newPlot" in body
    assert "net-worth-chart" in body


def test_spending_page_renders_with_plotly(client: TestClient) -> None:
    resp = client.get("/spending?months=3")
    assert resp.status_code == 200
    body = resp.text
    assert "Plotly.newPlot" in body
    assert "spending-chart" in body


def test_transactions_page_renders(client: TestClient) -> None:
    resp = client.get("/transactions")
    assert resp.status_code == 200
    body = resp.text
    assert "Spotify Premium" in body
    assert "Paycheck" in body


def test_transactions_rows_partial_filters_by_search(client: TestClient) -> None:
    resp = client.get("/transactions/rows?q=Spotify")
    assert resp.status_code == 200
    body = resp.text
    # Partial response: no <html>/<body>/<aside> wrappers from base.html.
    assert "<html" not in body.lower()
    assert "<aside" not in body.lower()
    # The matching row appears; the other one doesn't.
    assert "Spotify Premium" in body
    assert "Paycheck" not in body


def test_transactions_rows_partial_filters_by_account(client: TestClient) -> None:
    resp = client.get("/transactions/rows?account_id=acc-brokerage")
    assert resp.status_code == 200
    body = resp.text
    assert "Spotify Premium" not in body
    assert "No transactions match" in body


def test_sync_page_renders_with_warnings(client: TestClient) -> None:
    resp = client.get("/sync")
    assert resp.status_code == 200
    body = resp.text
    assert "Bank XYZ only returned 30 days" in body


def test_unconfigured_store_renders_empty_state(store: DuckDBStore) -> None:
    """An empty store still serves all pages without error."""
    app = build_app(store)
    with TestClient(app) as c:
        for path in (
            "/",
            "/net-worth",
            "/spending",
            "/spending-by-category",
            "/transactions",
            "/sync",
        ):
            r = c.get(path)
            assert r.status_code == 200, f"{path} returned {r.status_code}"


# --- Sub-seam 4: dashboard categorization surface --------------------------


def test_spending_by_category_page_renders(client: TestClient) -> None:
    """The seeded Spotify spending resolves to Subscriptions via the
    default rule; the page renders the chart and includes the category."""
    resp = client.get("/spending-by-category")
    assert resp.status_code == 200
    body = resp.text
    assert "Plotly.newPlot" in body
    assert "spending-by-category-chart" in body
    # Spotify → Subscriptions per the 0004 default rule seed.
    assert "Subscriptions" in body


def test_spending_by_category_page_renders_empty_state(store: DuckDBStore) -> None:
    """No transactions = empty state, not a broken page."""
    app = build_app(store)
    with TestClient(app) as c:
        resp = c.get("/spending-by-category")
    assert resp.status_code == 200
    assert "No spending" in resp.text


def test_transactions_page_renders_category_badge(client: TestClient) -> None:
    """Per-row category badge with the category text."""
    resp = client.get("/transactions")
    assert resp.status_code == 200
    body = resp.text
    # Spotify → Subscriptions (default rule); a badge element carries the text.
    assert "badge-category" in body
    assert "Subscriptions" in body


def test_transactions_page_badge_tooltip_has_cli_command_with_txn_id(
    client: TestClient,
) -> None:
    """The tooltip on the badge contains the pre-filled CLI command for
    that specific transaction id. Pin the literal — this is the
    affordance that the inline-edit-deferral relies on."""
    resp = client.get("/transactions")
    body = resp.text
    expected = 'title="goetta-finance transaction categorize tx-spotify &lt;new_category&gt;'
    assert expected in body, "badge tooltip must carry the pre-filled CLI command"


def test_transactions_page_supports_category_filter_param(
    client: TestClient,
) -> None:
    """`?category=Subscriptions` narrows results server-side via the view."""
    resp = client.get("/transactions?category=Subscriptions")
    assert resp.status_code == 200
    body = resp.text
    assert "Spotify Premium" in body
    # Paycheck has no rule match → resolves to 'Uncategorized', excluded by filter.
    assert "Paycheck" not in body


def test_transactions_rows_partial_supports_category_filter(
    client: TestClient,
) -> None:
    """HTMX partial endpoint honors the same category filter."""
    resp = client.get("/transactions/rows?category=Subscriptions")
    assert resp.status_code == 200
    body = resp.text
    assert "<html" not in body.lower()
    assert "Spotify Premium" in body
    assert "Paycheck" not in body


def test_transactions_page_has_category_filter_dropdown(client: TestClient) -> None:
    """The page renders a category <select> populated from store.get_categories()."""
    resp = client.get("/transactions")
    body = resp.text
    assert 'name="category"' in body
    # Default seeded category names appear as options.
    assert '<option value="Dining"' in body
    assert '<option value="Groceries"' in body


def test_base_template_has_spending_by_category_nav_link(client: TestClient) -> None:
    """Nav link to the new page appears on every page (lives in base.html)."""
    resp = client.get("/")
    assert resp.status_code == 200
    assert 'href="/spending-by-category"' in resp.text


# --- Migration 0005: hidden accounts ---------------------------------------


def test_accounts_page_hides_hidden_accounts(client: TestClient) -> None:
    """Hiding an account removes it from the Accounts page; net-worth
    drops by its contribution."""
    from goetta_finance.store.duckdb_store import DuckDBStore

    store = DuckDBStore(client.app.state.store.path)  # type: ignore[arg-type]
    try:
        store.set_account_hidden("acc-brokerage", True)
    finally:
        store.close()
    resp = client.get("/")
    assert resp.status_code == 200
    body = resp.text
    # The hidden Brokerage account is no longer in the main table.
    # (It still appears in the footer note. So check that it's not in
    # the data-table tbody, by looking for its balance — the Vanguard
    # 50000 line shouldn't appear as a row.)
    assert "Vanguard" not in body  # neither header nor footer mentions it by name
    # The footer note announces the exclusion.
    assert "1 hidden account" in body
    assert "Excludes" in body
    assert "50,000.00" in body  # the hidden balance in the footer note


def test_accounts_page_no_footer_note_when_no_hidden(client: TestClient) -> None:
    """Pin the negative case: with no hidden accounts the footer note
    doesn't render."""
    resp = client.get("/")
    assert resp.status_code == 200
    assert "Excludes" not in resp.text


def test_transactions_page_hides_hidden_account_txns(client: TestClient) -> None:
    """Default get_transactions_with_category filters transactions on
    hidden accounts. Hide the Checking account → its Spotify txn vanishes."""
    from goetta_finance.store.duckdb_store import DuckDBStore

    store = DuckDBStore(client.app.state.store.path)  # type: ignore[arg-type]
    try:
        store.set_account_hidden("acc-checking", True)
    finally:
        store.close()
    resp = client.get("/transactions")
    assert resp.status_code == 200
    assert "Spotify Premium" not in resp.text
    assert "Paycheck" not in resp.text
