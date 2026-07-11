"""MCP transfer-link tools: {ok, ...} contract and JSON shapes.

Direct-function style like test_curation_tools.py — the e2e tool-name
registration is pinned in test_server_e2e.py.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from goetta_finance.models import Account, AccountType, Transaction
from goetta_finance.store.duckdb_store import DuckDBStore
from goetta_finance.tools.transfer_links import (
    link_account_transfers,
    list_transfer_links,
    unlink_account_transfers,
)


def _seed(store: DuckDBStore) -> None:
    store.upsert_accounts(
        [
            Account(
                id="ACT-chk",
                org_name="Test Bank",
                name="Checking",
                balance=Decimal("6000.00"),
                balance_date=datetime(2026, 7, 1, tzinfo=UTC),
                type=AccountType.CHECKING,
            ),
            Account(
                id="MANUAL-sav",
                name="Apple Savings",
                balance=Decimal("10000.00"),
                balance_date=datetime(2026, 5, 21, tzinfo=UTC),
                type=AccountType.SAVINGS,
                is_manual=True,
            ),
        ]
    )
    store.upsert_transactions(
        [
            Transaction(
                id=f"t-{day}",
                account_id="ACT-chk",
                posted=datetime(2026, 6, day, 12, tzinfo=UTC),
                amount=Decimal("-500.00"),
                description="Web Authorized Pmt Apple Gs Savings",
                payee="Apple Savings",
            )
            for day in (5, 12)
        ]
    )


def test_link_tool_creates_and_applies(store: DuckDBStore) -> None:
    _seed(store)
    result = link_account_transfers(
        store,
        account_id="MANUAL-sav",
        source_account_id="ACT-chk",
        pattern="Apple Savings",
    )
    assert result["ok"] is True
    assert isinstance(result["link_id"], int)
    assert len(result["applied"]) == 1
    assert "+1000.00" in result["applied"][0]
    assert "true-up" in result["message"]


def test_link_tool_validation_and_store_errors(store: DuckDBStore) -> None:
    _seed(store)
    bad_match = link_account_transfers(
        store,
        account_id="MANUAL-sav",
        source_account_id="ACT-chk",
        pattern="x",
        match_type="glob",
    )
    assert bad_match["ok"] is False
    assert "link validation failed" in bad_match["error"]

    bad_regex = link_account_transfers(
        store,
        account_id="MANUAL-sav",
        source_account_id="ACT-chk",
        pattern="(a+)+",
        match_type="regex",
    )
    assert bad_regex["ok"] is False
    assert "nested quantifier" in bad_regex["error"]

    unknown = link_account_transfers(
        store,
        account_id="MANUAL-nope",
        source_account_id="ACT-chk",
        pattern="Apple Savings",
    )
    assert unknown["ok"] is False
    assert "account not found" in unknown["error"]


def test_list_tool_shape_and_suggestion_lifecycle(store: DuckDBStore) -> None:
    _seed(store)
    before = list_transfer_links(store)
    assert before["links"] == []
    [suggestion] = before["suggestions"]
    assert suggestion["payee"] == "Apple Savings"
    assert suggestion["transaction_count"] == 2
    assert suggestion["total"] == "1000.00"  # money as strings on the wire
    assert "account link MANUAL-sav" in suggestion["suggested_command"]

    link_account_transfers(
        store,
        account_id="MANUAL-sav",
        source_account_id="ACT-chk",
        pattern="Apple Savings",
    )
    after = list_transfer_links(store)
    assert after["suggestions"] == []
    [link] = after["links"]
    assert link["account_name"] == "Apple Savings"
    assert link["match_type"] == "contains"
    assert link["anchor"].startswith("2026-05-21")


def test_unlink_tool(store: DuckDBStore) -> None:
    _seed(store)
    created = link_account_transfers(
        store,
        account_id="MANUAL-sav",
        source_account_id="ACT-chk",
        pattern="Apple Savings",
    )
    result = unlink_account_transfers(store, created["link_id"])
    assert result["ok"] is True
    assert list_transfer_links(store)["links"] == []

    missing = unlink_account_transfers(store, 9999)
    assert missing["ok"] is False
    assert "not found" in missing["error"]
