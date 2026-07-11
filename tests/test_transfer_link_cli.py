"""CLI surface for transfer links: account link / links / unlink, plus
the set-balance true-up integration. Mirrors test_account_cli.py's
CliRunner + fresh_home conventions."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from typer.testing import CliRunner

from goetta_finance.cli import app
from goetta_finance.models import Account, AccountType, Transaction
from goetta_finance.store.duckdb_store import DuckDBStore

runner = CliRunner()


@pytest.fixture
def linked_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A GOETTA_FINANCE_HOME seeded with a synced checking account, a
    manual savings account, and two settled transfers toward it (one
    before the manual balance date, one after)."""
    monkeypatch.setenv("GOETTA_FINANCE_HOME", str(tmp_path))
    store = DuckDBStore(tmp_path / "data.duckdb")
    store.init()
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
                id="t-may",
                account_id="ACT-chk",
                posted=datetime(2026, 5, 15, 12, tzinfo=UTC),
                amount=Decimal("-500.00"),
                description="Web Authorized Pmt Apple Gs Savings",
                payee="Apple Savings",
            ),
            Transaction(
                id="t-june",
                account_id="ACT-chk",
                posted=datetime(2026, 6, 12, 12, tzinfo=UTC),
                amount=Decimal("-500.00"),
                description="Web Authorized Pmt Apple Gs Savings",
                payee="Apple Savings",
            ),
        ]
    )
    store.close()
    return tmp_path


def _savings_balance(home: Path) -> Decimal:
    store = DuckDBStore(home / "data.duckdb")
    try:
        return next(a.balance for a in store.get_accounts() if a.id == "MANUAL-sav")
    finally:
        store.close()


def test_account_link_creates_and_rolls_forward(linked_home: Path) -> None:
    result = runner.invoke(
        app,
        [
            "account",
            "link",
            "MANUAL-sav",
            "--from",
            "ACT-chk",
            "--pattern",
            "Apple Savings",
        ],
    )
    assert result.exit_code == 0, result.output
    assert "Linked:" in result.output
    # Only the post-balance-date transfer rolls forward.
    assert "+500.00" in result.output
    assert "10500.00" in result.output
    assert _savings_balance(linked_home) == Decimal("10500.00")


def test_account_link_rejects_bad_match_type(linked_home: Path) -> None:
    result = runner.invoke(
        app,
        [
            "account",
            "link",
            "MANUAL-sav",
            "--from",
            "ACT-chk",
            "--pattern",
            "x",
            "--match",
            "glob",
        ],
    )
    assert result.exit_code != 0
    assert "contains" in result.output


def test_account_link_refuses_synced_destination(linked_home: Path) -> None:
    result = runner.invoke(
        app,
        ["account", "link", "ACT-chk", "--from", "ACT-chk", "--pattern", "x"],
    )
    assert result.exit_code == 1
    assert "manual accounts only" in result.output


def test_account_links_lists_candidates_then_links(linked_home: Path) -> None:
    before = runner.invoke(app, ["account", "links"])
    assert before.exit_code == 0, before.output
    assert "No transfer links yet." in before.output
    assert "Detected candidates" in before.output
    assert 'account link MANUAL-sav --from ACT-chk --pattern "Apple Savings"' in before.output

    runner.invoke(
        app,
        ["account", "link", "MANUAL-sav", "--from", "ACT-chk", "--pattern", "Apple Savings"],
    )
    after = runner.invoke(app, ["account", "links"])
    assert after.exit_code == 0, after.output
    assert "Transfer links:" in after.output
    assert "Apple Savings <- Checking" in after.output
    assert "Detected candidates" not in after.output


def test_account_unlink(linked_home: Path) -> None:
    runner.invoke(
        app,
        ["account", "link", "MANUAL-sav", "--from", "ACT-chk", "--pattern", "Apple Savings"],
    )
    store = DuckDBStore(linked_home / "data.duckdb")
    try:
        [link] = store.list_transfer_links()
    finally:
        store.close()

    result = runner.invoke(app, ["account", "unlink", str(link.id)])
    assert result.exit_code == 0, result.output
    assert f"Removed transfer link {link.id}." in result.output

    missing = runner.invoke(app, ["account", "unlink", str(link.id)])
    assert missing.exit_code == 1
    assert "not found" in missing.output


def test_set_balance_true_up_reanchors_and_reapplies(linked_home: Path) -> None:
    runner.invoke(
        app,
        ["account", "link", "MANUAL-sav", "--from", "ACT-chk", "--pattern", "Apple Savings"],
    )
    assert _savings_balance(linked_home) == Decimal("10500.00")

    # Backdated true-up: balance as of June 1. The June 12 transfer
    # post-dates it, so it re-applies on top of the fresh number and the
    # command reports the final figure.
    result = runner.invoke(
        app,
        ["account", "set-balance", "MANUAL-sav", "11000", "--as-of", "2026-06-01"],
    )
    assert result.exit_code == 0, result.output
    assert "transfer:" in result.output
    assert "11500.00" in result.output
    assert _savings_balance(linked_home) == Decimal("11500.00")
