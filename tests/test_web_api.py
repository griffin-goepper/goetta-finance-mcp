"""Tests for the read-only JSON API (``/api/v1``) and the ``/dash`` mount.

The cent-pin tests are the load-bearing ones: the API's goal history and
month-by-category matrix must agree with the goal card / pie exactly,
because the companion SPA renders them side by side.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from goetta_finance.cli import app as cli_app
from goetta_finance.goals import spending_cap_history, spending_cap_progress
from goetta_finance.models import (
    Account,
    AccountType,
    BalanceSnapshot,
    SyncRun,
    Transaction,
)
from goetta_finance.server import build_server
from goetta_finance.store.duckdb_store import DuckDBStore
from goetta_finance.tools.goals import list_goals
from goetta_finance.tools.spending_by_category import query_spending_by_category
from goetta_finance.web.aggregations import (
    monthly_income_spending,
    monthly_spending_by_category,
)
from goetta_finance.web.app import build_app

runner = CliRunner()

NOW = datetime.now(tz=UTC)
MONTH_START = datetime(NOW.year, NOW.month, 1, tzinfo=UTC)
# A posted timestamp guaranteed inside the current UTC month, even when
# the test runs on the 1st: halfway between month start and now.
IN_MONTH = MONTH_START + (NOW - MONTH_START) / 2


def _seed(store: DuckDBStore) -> None:
    store.upsert_accounts(
        [
            Account(
                id="acc-checking",
                org_name="Chase",
                name="Checking 1234",
                balance=Decimal("2500.00"),
                balance_date=NOW,
                type=AccountType.CHECKING,
            ),
            Account(
                id="acc-card",
                org_name="Chase",
                name="Card 9876",
                balance=Decimal("-400.00"),  # SimpleFIN negative-CC convention
                balance_date=NOW,
                type=AccountType.CREDIT,
            ),
            Account(
                id="acc-secret",
                org_name="Chase",
                name="Hidden Savings",
                balance=Decimal("10000.00"),
                balance_date=NOW,
                type=AccountType.SAVINGS,
            ),
        ]
    )
    store.set_account_liability("acc-card", True)
    store.set_account_hidden("acc-secret", True)
    store.add_rule("Dining", match_type="contains", pattern="DINERTEST")
    store.upsert_transactions(
        [
            Transaction(
                id="tx-dining-settled",
                account_id="acc-checking",
                posted=IN_MONTH,
                amount=Decimal("-40.00"),
                description="DINERTEST downtown",
            ),
            Transaction(
                id="tx-dining-pending",
                account_id="acc-checking",
                posted=IN_MONTH,
                amount=Decimal("-15.50"),
                description="DINERTEST food truck",
                pending=True,
            ),
            Transaction(
                id="tx-dining-last-month",
                account_id="acc-checking",
                posted=MONTH_START - timedelta(days=10),
                amount=Decimal("-80.00"),
                description="DINERTEST uptown",
            ),
            Transaction(
                id="tx-hidden-dining",
                account_id="acc-secret",
                posted=IN_MONTH,
                amount=Decimal("-999.00"),
                description="DINERTEST hidden",
            ),
        ]
    )
    store.record_balance_snapshot(
        BalanceSnapshot(account_id="acc-checking", timestamp=NOW, balance=Decimal("2500.00"))
    )
    store.record_balance_snapshot(
        BalanceSnapshot(
            account_id="acc-card",
            timestamp=NOW - timedelta(days=20),
            balance=Decimal("-600.00"),
        )
    )
    store.record_balance_snapshot(
        BalanceSnapshot(account_id="acc-card", timestamp=NOW, balance=Decimal("-400.00"))
    )
    store.record_sync_run(
        SyncRun(
            started_at=NOW - timedelta(hours=2),
            finished_at=NOW - timedelta(hours=2) + timedelta(minutes=1),
            accounts_touched=3,
            transactions_new=4,
            warnings=["Bank XYZ only returned 30 days"],
        )
    )
    store.add_goal(
        "dining-cap",
        kind="spending_cap",
        amount=Decimal("50.00"),
        category_name="Dining",
        period="month",
    )
    store.add_goal(
        "card-paydown",
        kind="balance",
        amount=Decimal("200.00"),
        account_id="acc-card",
        direction="at_most",
    )


@pytest.fixture
def client(store: DuckDBStore) -> Iterator[TestClient]:
    _seed(store)
    with TestClient(build_app(store)) as c:
        yield c


def test_api_routes_win_over_mcp_mount(store: DuckDBStore) -> None:
    """The /api mount (MCP sub-app) must not swallow /api/v1/* — pins the
    register-api-before-mount ordering invariant in build_app."""
    _seed(store)
    mcp = build_server(store)
    app = build_app(store, mcp_server=mcp)
    with TestClient(app) as c:
        resp = c.get("/api/v1/accounts")
    assert resp.status_code == 200
    assert "accounts" in resp.json()


def test_api_accounts_money_as_strings_and_hidden_filter(client: TestClient) -> None:
    body = client.get("/api/v1/accounts").json()
    names = {a["name"] for a in body["accounts"]}
    assert "Hidden Savings" not in names
    checking = next(a for a in body["accounts"] if a["id"] == "acc-checking")
    assert checking["balance"] == "2500.00"  # str, never float
    body_all = client.get("/api/v1/accounts?include_hidden=true").json()
    assert {a["name"] for a in body_all["accounts"]} > names


def test_api_summary_signed_net_worth(client: TestClient) -> None:
    body = client.get("/api/v1/summary").json()
    # 2500 checking + (-abs(-400)) card; hidden savings excluded.
    assert body["net_worth"] == "2100.00"
    assert body["accounts_count"] == 2
    assert body["hidden_count"] == 1
    assert body["hidden_total"] == "10000.00"
    assert body["last_sync"] is not None


def test_api_net_worth_points(client: TestClient) -> None:
    body = client.get("/api/v1/net-worth?days=90").json()
    assert body["days"] == 90
    assert body["points"], "expected at least one net-worth point"
    last = body["points"][-1]
    # Latest snapshots: 2500 checking + (-abs(-400)) card = 2100.
    assert last["balance"] == "2100.00"
    datetime.fromisoformat(last["date"])  # ISO date


def test_api_cashflow_monthly_zero_fills(client: TestClient) -> None:
    body = client.get("/api/v1/cashflow/monthly?months=4").json()
    assert body["months"] == 4
    assert len(body["rows"]) == 4  # zero-filled buckets, oldest first
    assert body["rows"][0]["spending"] == "0.00" or body["rows"][0]["spending"] == "0"


def test_api_spending_by_category_window_and_color(client: TestClient) -> None:
    body = client.get("/api/v1/spending/by-category?days=400").json()
    dining = next(r for r in body["rows"] if r["category"] == "Dining")
    # 40 + 15.50 (pending counts) + 80 last month; hidden excluded.
    assert dining["total"] == "135.50"
    assert "color" in dining
    # Explicit range wins over days: a window covering only last month.
    # Date-only strings — a full ISO datetime's +00:00 offset must be
    # URL-encoded (unencoded + decodes to a space).
    start = (MONTH_START - timedelta(days=15)).date().isoformat()
    end = (MONTH_START - timedelta(days=5)).date().isoformat()
    body = client.get(f"/api/v1/spending/by-category?start={start}&end={end}").json()
    dining = next(r for r in body["rows"] if r["category"] == "Dining")
    assert dining["total"] == "80.00"


def test_api_malformed_date_is_400_not_silent_fallback(client: TestClient) -> None:
    """An explicit-but-unparseable date must not silently become the
    default window (the unencoded-+ trap)."""
    resp = client.get("/api/v1/spending/by-category?start=2026-06-20T00:00:00 00:00")
    assert resp.status_code == 400
    assert client.get("/api/v1/transactions?start=not-a-date").status_code == 400


def test_monthly_spending_by_category_matches_pie_single_month(store: DuckDBStore) -> None:
    """Cent pin: the matrix's current-month Dining bucket equals the
    shared pie helper over the same window."""
    _seed(store)
    matrix = monthly_spending_by_category(store, months=1)
    dining_row = next(r for r in matrix if r.category == "Dining")
    pie = query_spending_by_category(store, MONTH_START, NOW)
    pie_total = next(r["total"] for r in pie if r["category"] == "Dining")
    assert dining_row.total == pie_total


def test_monthly_spending_by_category_includes_pending(store: DuckDBStore) -> None:
    """Semantic pin: matrix counts pending (goal-cap semantics), the
    cashflow bars exclude it — a deliberate, documented divergence."""
    _seed(store)
    matrix = monthly_spending_by_category(store, months=1, category="Dining")
    assert matrix[-1].total == Decimal("55.50")  # 40.00 settled + 15.50 pending
    bars = monthly_income_spending(store, months=1)
    assert bars[-1].spending == Decimal("40.00")  # pending excluded


def test_monthly_spending_by_category_category_filter(store: DuckDBStore) -> None:
    _seed(store)
    rows = monthly_spending_by_category(store, months=12, category="Dining")
    assert {r.category for r in rows} == {"Dining"}
    assert len(rows) == 2  # current month + last month, sparse (no zero rows)


def test_api_goals_matches_list_goals_tool(client: TestClient, store: DuckDBStore) -> None:
    body = client.get("/api/v1/goals").json()
    assert body["goals"] == list_goals(store)


def test_spending_cap_history_current_period_matches_progress(store: DuckDBStore) -> None:
    """Cent pin: newest history bucket == the goal card's ``current``,
    including the pending transaction."""
    _seed(store)
    goal = next(g for g in store.list_goals() if g.name == "dining-cap")
    progress = spending_cap_progress(store, goal)
    history = spending_cap_history(store, goal, periods=3)
    assert len(history) == 3
    newest = history[-1]
    assert newest.actual == progress.current == Decimal("55.50")
    assert newest.period_start == progress.period_start
    assert newest.period_end == progress.period_end
    assert newest.over is True  # 55.50 >= 50.00 cap, same comparison as OVER
    # The prior bucket sees only the last-month transaction.
    assert history[-2].actual == Decimal("80.00")
    assert history[-3].actual == Decimal("0")


def test_api_goal_history_spending_cap_shape(client: TestClient, store: DuckDBStore) -> None:
    goal_id = next(g.id for g in store.list_goals() if g.name == "dining-cap")
    body = client.get(f"/api/v1/goals/{goal_id}/history?periods=3").json()
    assert body["kind"] == "spending_cap"
    assert body["period"] == "month"
    assert body["target"] == "50.00"
    assert len(body["periods"]) == 3
    assert body["periods"][-1]["actual"] == "55.50"
    assert body["periods"][-1]["over"] is True


def test_api_goal_history_balance_goal_points(client: TestClient, store: DuckDBStore) -> None:
    goal_id = next(g.id for g in store.list_goals() if g.name == "card-paydown")
    body = client.get(f"/api/v1/goals/{goal_id}/history").json()
    assert body["kind"] == "balance"
    assert body["direction"] == "at_most"
    # Liability: value is abs(balance) — amount owed.
    assert [p["value"] for p in body["points"]] == ["600.00", "400.00"]


def test_api_goal_history_unknown_id_404(client: TestClient) -> None:
    assert client.get("/api/v1/goals/99999/history").status_code == 404


def test_api_transactions_filters_and_search(client: TestClient) -> None:
    body = client.get("/api/v1/transactions?q=food+truck").json()
    assert body["count"] == 1
    assert body["transactions"][0]["id"] == "tx-dining-pending"
    assert body["transactions"][0]["amount"] == "-15.50"
    assert body["transactions"][0]["category"] == "Dining"
    # Hidden-account transactions excluded by default.
    all_body = client.get("/api/v1/transactions").json()
    assert "tx-hidden-dining" not in {t["id"] for t in all_body["transactions"]}


def test_api_transactions_limit_clamped(client: TestClient) -> None:
    body = client.get("/api/v1/transactions?limit=0").json()
    assert body["count"] == 1  # limit clamps up to 1


def test_api_categories(client: TestClient) -> None:
    body = client.get("/api/v1/categories").json()
    by_name = {c["name"]: c for c in body["categories"]}
    assert by_name["Dining"]["is_spending"] is True
    assert by_name["Transfers"]["is_spending"] is False
    assert "color" in by_name["Dining"]


def test_api_sync_status_parses_warnings(client: TestClient) -> None:
    body = client.get("/api/v1/sync/status").json()
    assert body["last_sync"] is not None
    run = body["runs"][0]
    assert run["warnings"] == ["Bank XYZ only returned 30 days"]
    assert run["errors"] == []
    assert run["transactions_new"] == 4


def test_dash_mount_serves_index_html(store: DuckDBStore, tmp_path: Path) -> None:
    dash = tmp_path / "dist"
    dash.mkdir()
    (dash / "index.html").write_text("<!doctype html><title>companion</title>", encoding="utf-8")
    with TestClient(build_app(store, dash_dir=dash)) as c:
        resp = c.get("/dash/")
        assert resp.status_code == 200
        assert "companion" in resp.text
        # No dash_dir -> no mount.
    with TestClient(build_app(store)) as c:
        assert c.get("/dash/").status_code == 404


def test_cli_rejects_bad_dash_dir(tmp_path: Path) -> None:
    missing = tmp_path / "nope"
    result = runner.invoke(cli_app, ["daemon", "--dash-dir", str(missing)])
    assert result.exit_code == 1
    assert "does not exist" in result.output
    empty = tmp_path / "empty"
    empty.mkdir()
    result = runner.invoke(cli_app, ["web", "--dash-dir", str(empty)])
    assert result.exit_code == 1
    assert "index.html" in result.output
