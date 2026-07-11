"""Tests for the MCP curation surface: categorize_transaction,
uncategorize_transaction, add_category_rule, remove_category_rule,
top_uncategorized_patterns.

Outcome-pinning: every test asserts on what the view returns (or what
rows exist) after the call, not on internal mechanics. The ReDoS tests
pin that the MCP write path runs the same validator as the CLI — the
CLAUDE.md threat model requires both surfaces gated identically.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from goetta_finance.models import Account, AccountType, Transaction
from goetta_finance.store.duckdb_store import DuckDBStore
from goetta_finance.tools.categorize import (
    add_category_rule,
    categorize_transaction,
    remove_category_rule,
    uncategorize_transaction,
)
from goetta_finance.tools.uncategorized import top_uncategorized_patterns


def _seed_one(store: DuckDBStore, txn_id: str = "cur-1", desc: str = "ZZZ UNMATCHED") -> None:
    store.upsert_accounts(
        [
            Account(
                id="cur-acc",
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
                id=txn_id,
                account_id="cur-acc",
                posted=datetime(2026, 5, 5, tzinfo=UTC),
                amount=Decimal("-10.00"),
                description=desc,
            )
        ]
    )


# --- categorize / uncategorize ----------------------------------------------


def test_categorize_transaction_sets_override(store: DuckDBStore) -> None:
    _seed_one(store)
    result = categorize_transaction(store, "cur-1", "Dining")
    assert result["ok"] is True
    rows = store.get_transactions_with_category()
    assert rows[0]["category"] == "Dining"


def test_categorize_transaction_unknown_category_suggests(store: DuckDBStore) -> None:
    _seed_one(store)
    result = categorize_transaction(store, "cur-1", "Dinning")
    assert result["ok"] is False
    assert 'Did you mean "Dining"' in result["error"]
    # No override written.
    rows = store.get_transactions_with_category()
    assert rows[0]["category"] == "Uncategorized"


def test_categorize_transaction_unknown_transaction_errors(store: DuckDBStore) -> None:
    result = categorize_transaction(store, "NO-SUCH-TXN", "Dining")
    assert result["ok"] is False
    assert "transaction not found" in result["error"].lower()


def test_uncategorize_transaction_falls_back_to_rule(store: DuckDBStore) -> None:
    _seed_one(store, desc="STARBUCKS STORE #1")  # legacy rule: Dining
    categorize_transaction(store, "cur-1", "Shopping")
    assert store.get_transactions_with_category()[0]["category"] == "Shopping"
    result = uncategorize_transaction(store, "cur-1")
    assert result["ok"] is True
    assert store.get_transactions_with_category()[0]["category"] == "Dining"


def test_uncategorize_transaction_idempotent(store: DuckDBStore) -> None:
    _seed_one(store)
    result = uncategorize_transaction(store, "cur-1")  # never had an override
    assert result["ok"] is True


# --- add_category_rule -------------------------------------------------------


def test_add_category_rule_round_trip_retroactive(store: DuckDBStore) -> None:
    """Rule added AFTER the transaction exists still categorizes it —
    the read-time-resolution contract through the MCP write path."""
    _seed_one(store, desc="DUKEENERGY PAYMENT")
    assert store.get_transactions_with_category()[0]["category"] == "Uncategorized"
    result = add_category_rule(store, "Utilities", "contains", "Dukeenergy")
    assert result["ok"] is True
    assert isinstance(result["rule_id"], int)
    assert store.get_transactions_with_category()[0]["category"] == "Utilities"


def test_add_category_rule_rejects_redos(store: DuckDBStore) -> None:
    """MCP path refuses the same ReDoS shapes as the CLI — identical
    gate on both write surfaces per the CLAUDE.md threat model."""
    before = store.conn.execute("SELECT COUNT(*) FROM category_rules").fetchone()
    result = add_category_rule(store, "Dining", "regex", "(a+)+$")
    assert result["ok"] is False
    assert "validation failed" in result["error"]
    after = store.conn.execute("SELECT COUNT(*) FROM category_rules").fetchone()
    assert before is not None and after is not None and before[0] == after[0]


def test_add_category_rule_rejects_uncompilable(store: DuckDBStore) -> None:
    result = add_category_rule(store, "Dining", "regex", "[")
    assert result["ok"] is False
    assert "did not compile" in result["error"]


def test_add_category_rule_unknown_category_suggests(store: DuckDBStore) -> None:
    result = add_category_rule(store, "Dinning", "contains", "SOMETHING")
    assert result["ok"] is False
    assert 'Did you mean "Dining"' in result["error"]


def test_add_category_rule_normalizes_match_type_case(store: DuckDBStore) -> None:
    result = add_category_rule(store, "Dining", "CONTAINS", "ZZZ-CASE-TEST")
    assert result["ok"] is True


# --- remove_category_rule ------------------------------------------------------


def test_remove_category_rule_round_trip_retroactive(store: DuckDBStore) -> None:
    """Removing a rule un-categorizes its matches on the next read — the
    read-time-resolution contract, exercised through the MCP write path."""
    _seed_one(store, desc="DUKEENERGY PAYMENT")
    added = add_category_rule(store, "Utilities", "contains", "Dukeenergy")
    assert store.get_transactions_with_category()[0]["category"] == "Utilities"
    result = remove_category_rule(store, added["rule_id"])
    assert result["ok"] is True
    assert store.get_transactions_with_category()[0]["category"] == "Uncategorized"


def test_remove_category_rule_falls_back_to_remaining_rule(store: DuckDBStore) -> None:
    """When two rules match, removing the winner (lower priority number)
    re-resolves through the survivor rather than to Uncategorized."""
    _seed_one(store, desc="WALGREENS #123")
    winner = add_category_rule(store, "Healthcare", "contains", "WALGREENS", priority=15)
    add_category_rule(store, "Shopping", "contains", "WALGREENS", priority=20)
    assert store.get_transactions_with_category()[0]["category"] == "Healthcare"
    result = remove_category_rule(store, winner["rule_id"])
    assert result["ok"] is True
    assert store.get_transactions_with_category()[0]["category"] == "Shopping"


def test_remove_category_rule_refuses_default(store: DuckDBStore) -> None:
    """Defaults are CLI-only (typed --force confirmation); the MCP surface
    has no force parameter by design — prompt-injection threat model."""
    default_row = store.conn.execute(
        "SELECT id FROM category_rules WHERE is_default = TRUE LIMIT 1"
    ).fetchone()
    assert default_row is not None
    rule_id = int(default_row[0])
    result = remove_category_rule(store, rule_id)
    assert result["ok"] is False
    assert "--force" in result["error"]  # points the user at the CLI path
    still = store.conn.execute(
        "SELECT COUNT(*) FROM category_rules WHERE id = ?", [rule_id]
    ).fetchone()
    assert still is not None and still[0] == 1


def test_remove_category_rule_unknown_id(store: DuckDBStore) -> None:
    result = remove_category_rule(store, 999999)
    assert result["ok"] is False
    assert "not found" in result["error"].lower()


# --- top_uncategorized_patterns ----------------------------------------------


def _seed_uncat(store: DuckDBStore, rows: list[tuple[str, str, str]], *, now: datetime) -> None:
    """rows = [(txn_id, description, amount_str)] posted 3 days before now."""
    store.upsert_accounts(
        [
            Account(
                id="uncat-acc",
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
                id=tid,
                account_id="uncat-acc",
                posted=now - timedelta(days=3),
                amount=Decimal(amt),
                description=desc,
            )
            for tid, desc, amt in rows
        ]
    )


def test_top_uncategorized_patterns_groups_and_sorts(
    store: DuckDBStore, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("GOETTA_FINANCE_HOME", str(tmp_path))
    now = datetime(2026, 6, 1, tzinfo=UTC)
    _seed_uncat(
        store,
        [
            ("u1", "VENMO LES ROGER", "-2700.00"),
            ("u2", "VENMO LES ROGER", "-2700.00"),
            ("u3", "COFFEE PLACE", "-5.00"),
        ],
        now=now,
    )
    result = top_uncategorized_patterns(store, days=30, top=10, now=now)
    assert result[0]["pattern"] == "VENMO LES"
    assert result[0]["transaction_count"] == 2
    assert Decimal(result[0]["total"]) == Decimal("5400.00")
    assert result[1]["pattern"] == "COFFEE PLACE"


def test_top_uncategorized_patterns_strips_processor_prefixes(
    store: DuckDBStore, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """TST* / AplPay prefixes normalize away (universal defaults) so the
    same merchant doesn't fragment into multiple rows."""
    monkeypatch.setenv("GOETTA_FINANCE_HOME", str(tmp_path))
    now = datetime(2026, 6, 1, tzinfo=UTC)
    _seed_uncat(
        store,
        [
            ("p1", "TST* GHOST KITCHEN CINCI", "-20.00"),
            ("p2", "GHOST KITCHEN CINCI", "-30.00"),
            ("p3", "AplPay SP GHOST KITCHEN CINCI", "-10.00"),
        ],
        now=now,
    )
    result = top_uncategorized_patterns(store, days=30, top=10, now=now)
    assert len(result) == 1
    assert result[0]["pattern"] == "GHOST KITCHEN"
    assert result[0]["transaction_count"] == 3


def test_top_uncategorized_patterns_honors_user_prefix_file(
    store: DuckDBStore, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A bank-template prefix added to prefixes.txt is stripped. This is
    the stranger-test extension path: the codebase ships processor
    prefixes only; bank templates are user config."""
    monkeypatch.setenv("GOETTA_FINANCE_HOME", str(tmp_path))
    (tmp_path / "prefixes.txt").write_text("Web Authorized Pmt\\s*\n", encoding="utf-8")
    now = datetime(2026, 6, 1, tzinfo=UTC)
    _seed_uncat(
        store,
        [
            ("b1", "Web Authorized Pmt Some Biller", "-50.00"),
            ("b2", "Some Biller", "-25.00"),
        ],
        now=now,
    )
    result = top_uncategorized_patterns(store, days=30, top=10, now=now)
    assert len(result) == 1
    assert result[0]["pattern"] == "SOME BILLER"


def test_top_uncategorized_patterns_excludes_categorized(
    store: DuckDBStore, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("GOETTA_FINANCE_HOME", str(tmp_path))
    now = datetime(2026, 6, 1, tzinfo=UTC)
    _seed_uncat(
        store,
        [
            ("c1", "STARBUCKS STORE", "-10.00"),  # legacy rule: Dining
            ("c2", "MYSTERY MERCHANT", "-20.00"),
        ],
        now=now,
    )
    result = top_uncategorized_patterns(store, days=30, top=10, now=now)
    patterns = {r["pattern"] for r in result}
    assert "MYSTERY MERCHANT" in patterns
    assert not any("STARBUCKS" in p for p in patterns)


def test_top_uncategorized_patterns_excludes_hidden_window_and_refunds(
    store: DuckDBStore, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("GOETTA_FINANCE_HOME", str(tmp_path))
    now = datetime(2026, 6, 1, tzinfo=UTC)
    _seed_uncat(store, [("w1", "RECENT MERCHANT", "-10.00")], now=now)
    store.upsert_transactions(
        [
            # Outside the 30-day window — excluded.
            Transaction(
                id="w-old",
                account_id="uncat-acc",
                posted=now - timedelta(days=90),
                amount=Decimal("-99.00"),
                description="OLD MERCHANT",
            ),
            # Positive amount (refund) — excluded by amount < 0.
            Transaction(
                id="w-refund",
                account_id="uncat-acc",
                posted=now - timedelta(days=2),
                amount=Decimal("15.00"),
                description="REFUND MERCHANT",
            ),
        ]
    )
    # Hidden account filters everything.
    store.set_account_hidden("uncat-acc", True)
    assert top_uncategorized_patterns(store, days=30, top=10, now=now) == []
    # Unhidden: only the recent, negative-amount transaction shows.
    store.set_account_hidden("uncat-acc", False)
    result = top_uncategorized_patterns(store, days=30, top=10, now=now)
    assert {r["pattern"] for r in result} == {"RECENT MERCHANT"}


def test_top_uncategorized_patterns_honors_top_n(
    store: DuckDBStore, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("GOETTA_FINANCE_HOME", str(tmp_path))
    now = datetime(2026, 6, 1, tzinfo=UTC)
    _seed_uncat(
        store,
        [(f"n{i}", f"MERCHANT NUMBER{i}", f"-{10 + i}.00") for i in range(6)],
        now=now,
    )
    result = top_uncategorized_patterns(store, days=30, top=3, now=now)
    assert len(result) == 3
    # Sorted descending: the most expensive merchants survive the cut.
    assert Decimal(result[0]["total"]) >= Decimal(result[1]["total"])
    assert Decimal(result[1]["total"]) >= Decimal(result[2]["total"])


def test_top_uncategorized_patterns_suggested_command_shape(
    store: DuckDBStore, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("GOETTA_FINANCE_HOME", str(tmp_path))
    now = datetime(2026, 6, 1, tzinfo=UTC)
    _seed_uncat(store, [("s1", "NEW GYM LLC", "-45.00")], now=now)
    result = top_uncategorized_patterns(store, days=30, top=10, now=now)
    cmd = result[0]["suggested_command"]
    assert cmd.startswith("goetta-finance category set-rule ")
    assert '--pattern "NEW GYM"' in cmd
