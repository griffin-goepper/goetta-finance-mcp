from __future__ import annotations

import os
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest

from goetta_finance.models import Account, AccountType, BalanceSnapshot, Transaction
from goetta_finance.store.duckdb_store import DuckDBStore
from goetta_finance.tools._serialize import serialize_value
from goetta_finance.tools.accounts import list_accounts
from goetta_finance.tools.balance_history import account_balance_history
from goetta_finance.tools.goals import list_goals, remove_goal, set_goal
from goetta_finance.tools.set_account_balance import set_account_balance
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


def test_get_transactions_carries_pending_flag(store: DuckDBStore) -> None:
    _seed(store)
    store.upsert_transactions(
        [
            Transaction(
                id="t-pending",
                account_id="a1",
                posted=datetime(2026, 5, 12, tzinfo=UTC),
                amount=Decimal("-15.99"),
                description="Pending hold",
                pending=True,
            )
        ]
    )
    by_id = {t["id"]: t for t in get_transactions(store)}
    assert by_id["t-pending"]["pending"] is True
    assert by_id["t1"]["pending"] is False


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


def test_serialize_value_conversions() -> None:
    """Decimal -> str, datetime/date -> isoformat, everything else untouched."""
    assert serialize_value(Decimal("12.50")) == "12.50"
    assert serialize_value(datetime(2026, 5, 1, 12, 30, tzinfo=UTC)) == (
        "2026-05-01T12:30:00+00:00"
    )
    assert serialize_value(date(2026, 5, 1)) == "2026-05-01"
    assert serialize_value("plain") == "plain"
    assert serialize_value(42) == 42
    assert serialize_value(None) is None


def test_goal_tools_error_shapes(store: DuckDBStore) -> None:
    """Write tools return {ok: false, error} — never raise — so Claude
    can read the outcome and self-correct."""
    assert remove_goal(store, 999) == {"ok": False, "error": "goal not found: 999"}
    missing_fields = set_goal(store, name="x", kind="balance", amount=Decimal("100"))
    assert missing_fields["ok"] is False
    assert "validation failed" in missing_fields["error"]
    bad_amount = set_goal(
        store,
        name="x",
        kind="spending_cap",
        amount=Decimal("9.999"),
        category="Dining",
        period="month",
    )
    assert bad_amount["ok"] is False
    assert "sub-cent" in bad_amount["error"]


def test_set_goal_contribution_round_trip_and_list_fields(store: DuckDBStore) -> None:
    """MCP write surface for the new kind: match_type defaults to
    'contains' when only a pattern is given, and list_goals carries the
    four definition fields on EVERY entry (null for other kinds)."""
    _seed(store)
    result = set_goal(
        store,
        name="Roth IRA 2026",
        kind="contribution",
        amount=Decimal("7500.00"),
        account_id="a2",
        period="year",
        match_pattern="CASH CONTRIBUTION CURRENT YEAR",
        baseline_amount=Decimal("3000.00"),
        baseline_date="2026-03-01",
    )
    assert result["ok"] is True, result
    cap = set_goal(
        store,
        name="Dining cap",
        kind="spending_cap",
        amount=Decimal("400.00"),
        category="Dining",
        period="month",
    )
    assert cap["ok"] is True
    goals = {g["name"]: g for g in list_goals(store)}
    contrib = goals["Roth IRA 2026"]
    assert contrib["kind"] == "contribution"
    assert contrib["match_type"] == "contains"  # defaulted from the pattern
    assert contrib["match_pattern"] == "CASH CONTRIBUTION CURRENT YEAR"
    assert contrib["baseline_amount"] == "3000.00"
    assert contrib["baseline_date"] == "2026-03-01T00:00:00+00:00"
    assert contrib["category"] is None
    assert contrib["direction"] is None
    assert contrib["target_date"] is None
    assert contrib["monthly_delta"] is None
    assert contrib["projected_date"] is None
    # Non-contribution entries carry the same four keys, all null.
    dining = goals["Dining cap"]
    assert dining["match_type"] is None
    assert dining["match_pattern"] is None
    assert dining["baseline_amount"] is None
    assert dining["baseline_date"] is None


def test_set_goal_contribution_validation_errors(store: DuckDBStore) -> None:
    """The MCP surface is gated identically to the CLI: shared
    validate_rule_pattern refuses ReDoS shapes, the baseline pair and
    match pair are enforced, and match_type alone is refused."""
    _seed(store)
    redos = set_goal(
        store,
        name="evil",
        kind="contribution",
        amount=Decimal("100"),
        account_id="a2",
        period="month",
        match_type="regex",
        match_pattern="(a+)+$",
    )
    assert redos["ok"] is False
    assert "nested quantifier" in redos["error"]
    half_baseline = set_goal(
        store,
        name="half",
        kind="contribution",
        amount=Decimal("100"),
        account_id="a2",
        period="month",
        match_pattern="X",
        baseline_amount=Decimal("50"),
    )
    assert half_baseline["ok"] is False
    assert "provided together" in half_baseline["error"]
    lone_match_type = set_goal(
        store,
        name="lonely type",
        kind="contribution",
        amount=Decimal("100"),
        account_id="a2",
        period="month",
        match_type="contains",
    )
    assert lone_match_type["ok"] is False
    assert "requires a match_pattern" in lone_match_type["error"]
    synced_no_pattern = set_goal(
        store,
        name="no matcher",
        kind="contribution",
        amount=Decimal("100"),
        account_id="a2",
        period="month",
    )
    assert synced_no_pattern["ok"] is False
    assert "need a match_pattern" in synced_no_pattern["error"]


def test_set_goal_recurring_round_trip_and_list_fields(store: DuckDBStore) -> None:
    """MCP surface for the declared schedule (0015): interval defaults
    to 'biweekly', the anchor parses from ISO, and list_goals carries
    the triple on every entry (null on other kinds)."""
    _seed(store)
    result = set_goal(
        store,
        name="HSA 2026",
        kind="contribution",
        amount=Decimal("4400.00"),
        account_id="a2",
        period="year",
        match_pattern="EMPLOYER CONTRIBUTION",
        recurring_amount=Decimal("150.00"),
        recurring_anchor="2026-01-09",
    )
    assert result["ok"] is True, result
    cap = set_goal(
        store,
        name="Dining cap 2",
        kind="spending_cap",
        amount=Decimal("400.00"),
        category="Dining",
        period="month",
    )
    assert cap["ok"] is True
    goals = {g["name"]: g for g in list_goals(store)}
    hsa = goals["HSA 2026"]
    assert hsa["recurring_amount"] == "150.00"
    assert hsa["recurring_interval"] == "biweekly"  # defaulted
    assert hsa["recurring_anchor"] == "2026-01-09"
    dining = goals["Dining cap 2"]
    assert dining["recurring_amount"] is None
    assert dining["recurring_interval"] is None
    assert dining["recurring_anchor"] is None


def test_set_goal_recurring_validation_errors(store: DuckDBStore) -> None:
    _seed(store)
    partial = set_goal(
        store,
        name="half schedule",
        kind="contribution",
        amount=Decimal("100"),
        account_id="a2",
        period="month",
        match_pattern="X",
        recurring_amount=Decimal("50"),
    )
    assert partial["ok"] is False
    assert "provided together" in partial["error"]
    bad_interval = set_goal(
        store,
        name="bad interval",
        kind="contribution",
        amount=Decimal("100"),
        account_id="a2",
        period="month",
        match_pattern="X",
        recurring_amount=Decimal("50"),
        recurring_interval="fortnightly",
        recurring_anchor="2026-01-09",
    )
    assert bad_interval["ok"] is False
    assert "biweekly" in bad_interval["error"]
    on_balance = set_goal(
        store,
        name="balance recurring",
        kind="balance",
        amount=Decimal("100"),
        account_id="a2",
        direction="at_least",
        recurring_amount=Decimal("50"),
        recurring_interval="biweekly",
        recurring_anchor="2026-01-09",
    )
    assert on_balance["ok"] is False
    assert "only apply to contribution" in on_balance["error"]


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


def test_spending_by_category_excludes_non_spending_by_default(
    store: DuckDBStore,
) -> None:
    """Categories with is_spending=FALSE (Transfers, Income, any user-
    flagged ones) don't appear in the default result. Migration 0006
    seeded Transfers and Income as non-spending."""
    _seed_cat(store)
    # Seed a Transfers transaction in addition to the Income one from _seed_cat.
    store.upsert_transactions(
        [
            Transaction(
                id="t-xfer",
                account_id="cat-a1",
                posted=datetime(2026, 5, 20, tzinfo=UTC),
                amount=Decimal("-500.00"),
                description="ZZZ-XFER-DESC",
            )
        ]
    )
    store.set_transaction_override("t-xfer", "Transfers")

    rows = spending_by_category(
        store, datetime(2026, 5, 1, tzinfo=UTC), datetime(2026, 5, 31, tzinfo=UTC)
    )
    cats = {r["category"] for r in rows}
    assert "Income" not in cats
    assert "Transfers" not in cats


def test_spending_by_category_includes_non_spending_with_opt_in(
    store: DuckDBStore,
) -> None:
    """include_non_spending=True surfaces both Income (negative total —
    cash in) and Transfers (positive total — outflow on the source
    account). Income's sign conveys direction."""
    _seed_cat(store)
    store.upsert_transactions(
        [
            Transaction(
                id="t-xfer-2",
                account_id="cat-a1",
                posted=datetime(2026, 5, 20, tzinfo=UTC),
                amount=Decimal("-500.00"),
                description="ZZZ-XFER-DESC2",
            )
        ]
    )
    store.set_transaction_override("t-xfer-2", "Transfers")

    rows = spending_by_category(
        store,
        datetime(2026, 5, 1, tzinfo=UTC),
        datetime(2026, 5, 31, tzinfo=UTC),
        include_non_spending=True,
    )
    by_cat = {r["category"]: r for r in rows}
    income = by_cat["Income"]
    transfers = by_cat["Transfers"]
    # Income: positive-amount paycheck → SUM(-amount) is negative.
    assert Decimal(income["total"]) == Decimal("-3000.00")
    # Transfers: negative-amount outflow → SUM(-amount) is positive.
    assert Decimal(transfers["total"]) == Decimal("500.00")


def test_spending_by_category_rule_resolved_refund_net_reduces(
    store: DuckDBStore,
) -> None:
    """A POSITIVE-amount refund whose category resolves via the default
    RULE for STARBUCKS → Dining NET-REDUCES the Dining total (net
    spending). Exercises the matched_rule branch of the view (no
    override row)."""
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
    assert dining["total"] == "8.50"  # 12.50 - 4.00 net (refund subtracts)
    assert dining["transaction_count"] == 2


def test_spending_by_category_override_resolved_refund_net_reduces(
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
    assert dining["total"] == "30.00"  # 50.00 - 20.00 net (refund subtracts)
    assert dining["transaction_count"] == 2


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


def test_spending_by_category_excludes_hidden_account_transactions(
    store: DuckDBStore,
) -> None:
    """A transaction on a hidden account must not contribute to category
    totals. The Fidelity-duplicate use case has no transactions, but
    other hidden accounts (e.g. an old closed checking) might — those
    must not pollute totals."""
    store.upsert_accounts(
        [
            Account(
                id="cat-vis-acc",
                org_name="Visible",
                name="Checking",
                balance=Decimal("100.00"),
                balance_date=datetime(2026, 5, 1, tzinfo=UTC),
                type=AccountType.CHECKING,
            ),
            Account(
                id="cat-hid-acc",
                org_name="Old",
                name="Hidden Checking",
                balance=Decimal("0.00"),
                balance_date=datetime(2026, 5, 1, tzinfo=UTC),
                type=AccountType.CHECKING,
            ),
        ]
    )
    store.upsert_transactions(
        [
            Transaction(
                id="cat-vis-txn",
                account_id="cat-vis-acc",
                posted=datetime(2026, 5, 5, tzinfo=UTC),
                amount=Decimal("-25.00"),
                description="STARBUCKS STORE #1",
            ),
            Transaction(
                id="cat-hid-txn",
                account_id="cat-hid-acc",
                posted=datetime(2026, 5, 6, tzinfo=UTC),
                amount=Decimal("-200.00"),  # would dominate Dining if counted
                description="STARBUCKS STORE #2",
            ),
        ]
    )
    store.set_account_hidden("cat-hid-acc", True)

    rows = spending_by_category(
        store, datetime(2026, 5, 1, tzinfo=UTC), datetime(2026, 5, 31, tzinfo=UTC)
    )
    dining = next(r for r in rows if r["category"] == "Dining")
    # Only the visible-account transaction contributes; the hidden one is excluded.
    assert dining["total"] == "25.00"
    assert dining["transaction_count"] == 1


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


@pytest.mark.skipif(
    os.environ.get("CI") is not None,
    reason=(
        "Absolute-threshold perf probe: the 30ms baseline is calibrated for local "
        "hardware. Shared CI runners (esp. Windows) are several times slower and "
        "trip the gate with false regressions (~220ms observed). Runs locally, "
        "where the threshold is meaningful."
    ),
)
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
    # The raw in-engine INSERT bypasses upsert_transactions' write-through
    # cache maintenance (migration 0013), so rebuild explicitly — exactly
    # the documented use of the public rebuild method.
    store.rebuild_category_match_cache()

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


# --- set_account_balance (manual true-up over MCP) --------------------------


def _seed_manual(store: DuckDBStore) -> None:
    store.upsert_accounts(
        [
            Account(
                id="tu-chk",
                org_name="Bank",
                name="Checking",
                balance=Decimal("100.00"),
                balance_date=datetime(2026, 5, 1, tzinfo=UTC),
                type=AccountType.CHECKING,
            ),
            Account(
                id="MANUAL-tu",
                name="Apple Savings",
                balance=Decimal("25000.00"),
                balance_date=datetime(2026, 5, 1, tzinfo=UTC),
                type=AccountType.SAVINGS,
                is_manual=True,
            ),
        ]
    )


def test_set_account_balance_updates_balance_and_snapshot(store: DuckDBStore) -> None:
    _seed_manual(store)
    result = set_account_balance(
        store, account="MANUAL-tu", balance=Decimal("30450.12"), as_of="2026-06-01"
    )
    assert result["ok"] is True, result
    # Serialized like list_accounts: money as strings.
    assert result["account"]["id"] == "MANUAL-tu"
    assert result["account"]["balance"] == "30450.12"
    assert result["account"]["balance_date"].startswith("2026-06-01")
    assert result["snapshot"] == {
        "account_id": "MANUAL-tu",
        "balance": "30450.12",
        "timestamp": "2026-06-01T00:00:00+00:00",
    }
    assert result["links_reanchored"] == 0
    assert result["transfers_reapplied"] == []
    acc = next(a for a in store.get_accounts() if a.id == "MANUAL-tu")
    assert acc.balance == Decimal("30450.12")
    assert acc.balance_date == datetime(2026, 6, 1, tzinfo=UTC)
    snaps = store.conn.execute(
        "SELECT balance FROM balance_snapshots WHERE account_id = 'MANUAL-tu'"
    ).fetchall()
    assert [r[0] for r in snaps] == [Decimal("30450.12")]


def test_set_account_balance_resolves_name_case_insensitively(store: DuckDBStore) -> None:
    _seed_manual(store)
    result = set_account_balance(store, account="apple savings", balance=Decimal("31000.00"))
    assert result["ok"] is True, result
    assert result["account"]["id"] == "MANUAL-tu"


def test_set_account_balance_refuses_non_manual(store: DuckDBStore) -> None:
    _seed_manual(store)
    result = set_account_balance(store, account="tu-chk", balance=Decimal("1.00"))
    assert result["ok"] is False
    assert "non-manual" in result["error"]
    assert "SimpleFIN" in result["error"]
    # Nothing written.
    acc = next(a for a in store.get_accounts() if a.id == "tu-chk")
    assert acc.balance == Decimal("100.00")


def test_set_account_balance_unknown_name_gives_did_you_mean(store: DuckDBStore) -> None:
    _seed_manual(store)
    result = set_account_balance(store, account="Aple Savings", balance=Decimal("1.00"))
    assert result["ok"] is False
    assert result["error"].startswith("account not found: Aple Savings.")
    assert 'Did you mean "Apple Savings"?' in result["error"]


def test_set_account_balance_rejects_bad_and_future_as_of(store: DuckDBStore) -> None:
    _seed_manual(store)
    bad = set_account_balance(
        store, account="MANUAL-tu", balance=Decimal("1.00"), as_of="06/01/2026"
    )
    assert bad["ok"] is False
    assert "ISO date" in bad["error"]

    future = set_account_balance(
        store, account="MANUAL-tu", balance=Decimal("1.00"), as_of="2999-01-01"
    )
    assert future["ok"] is False
    assert "future" in future["error"]
    # Neither attempt wrote anything.
    acc = next(a for a in store.get_accounts() if a.id == "MANUAL-tu")
    assert acc.balance == Decimal("25000.00")


def test_set_account_balance_reports_reanchor_and_reapply(store: DuckDBStore) -> None:
    """A true-up on a linked account re-anchors the links and re-applies
    transfers posted after as_of; the returned account carries the final
    (post-reapply) balance while the snapshot carries the declared one."""
    _seed_manual(store)
    store.upsert_transactions(
        [
            Transaction(
                id="tu-t1",
                account_id="tu-chk",
                posted=datetime(2026, 6, 12, 12, tzinfo=UTC),
                amount=Decimal("-500.00"),
                description="Transfer to savings",
                payee="Apple Savings",
            )
        ]
    )
    store.add_transfer_link("MANUAL-tu", "tu-chk", match_type="contains", pattern="Apple Savings")
    # Link creation already applied tu-t1 (posted after the May 1 balance
    # date); a backdated June 1 true-up must release and re-apply it.
    result = set_account_balance(
        store, account="MANUAL-tu", balance=Decimal("31000.00"), as_of="2026-06-01"
    )
    assert result["ok"] is True, result
    assert result["links_reanchored"] == 1
    assert len(result["transfers_reapplied"]) == 1
    assert "+500.00" in result["transfers_reapplied"][0]
    assert result["snapshot"]["balance"] == "31000.00"
    assert result["account"]["balance"] == "31500.00"
    acc = next(a for a in store.get_accounts() if a.id == "MANUAL-tu")
    assert acc.balance == Decimal("31500.00")
