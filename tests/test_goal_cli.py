"""CLI tests for the `goal` command group and the post-sync breach lines.

Follows the test_category_cli.py pattern: GOETTA_FINANCE_HOME pointed
at a tmp dir with a migrated DuckDB; the CLI reopens the store itself.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from typer.testing import CliRunner

from goetta_finance.cli import app
from goetta_finance.models import Account, AccountType, SyncRun, Transaction
from goetta_finance.store.duckdb_store import DuckDBStore

runner = CliRunner()


@pytest.fixture
def fresh_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("GOETTA_FINANCE_HOME", str(tmp_path))
    store = DuckDBStore(tmp_path / "data.duckdb")
    store.init()
    store.close()
    return tmp_path


def _seed_account(home: Path, *, account_id: str = "ACT-goal", liability: bool = False) -> None:
    store = DuckDBStore(home / "data.duckdb")
    try:
        store.upsert_accounts(
            [
                Account(
                    id=account_id,
                    org_name="Test",
                    name="Goal Checking",
                    balance=Decimal("6500.00"),
                    balance_date=datetime.now(tz=UTC),
                    type=AccountType.CHECKING,
                )
            ]
        )
        if liability:
            store.set_account_liability(account_id, True)
    finally:
        store.close()


def _seed_current_month_spend(home: Path, *, amount: str, category: str = "Dining") -> None:
    """A transaction posted now, overridden to CATEGORY, so goal math
    (which evaluates the current calendar bucket) sees it."""
    store = DuckDBStore(home / "data.duckdb")
    try:
        store.upsert_transactions(
            [
                Transaction(
                    id=f"ACT-tx-{category}-{amount}",
                    account_id="ACT-goal",
                    posted=datetime.now(tz=UTC),
                    amount=Decimal(amount),
                    description="cli goal test txn",
                )
            ]
        )
        store.set_transaction_override(f"ACT-tx-{category}-{amount}", category)
    finally:
        store.close()


# --- add-spending -----------------------------------------------------------


def test_goal_add_spending_happy_path(fresh_home: Path) -> None:
    result = runner.invoke(
        app,
        ["goal", "add-spending", "Dining", "--limit", "400", "--period", "month"],
    )
    assert result.exit_code == 0, result.output
    assert 'Added goal "Dining under 400/month"' in result.output
    assert "Dining under 400 per month" in result.output


def test_goal_add_spending_custom_name(fresh_home: Path) -> None:
    result = runner.invoke(
        app,
        ["goal", "add-spending", "Groceries", "--limit", "500", "--name", "Food budget"],
    )
    assert result.exit_code == 0, result.output
    assert 'Added goal "Food budget"' in result.output


def test_goal_add_spending_unknown_category_did_you_mean(fresh_home: Path) -> None:
    result = runner.invoke(app, ["goal", "add-spending", "Gorceries", "--limit", "400"])
    assert result.exit_code == 1
    assert "category not found" in result.output
    assert 'Did you mean "Groceries"?' in result.output


def test_goal_add_spending_bad_period(fresh_home: Path) -> None:
    result = runner.invoke(
        app, ["goal", "add-spending", "Dining", "--limit", "400", "--period", "week"]
    )
    assert result.exit_code == 2
    assert "month" in result.output and "year" in result.output


def test_goal_add_spending_bad_limit(fresh_home: Path) -> None:
    result = runner.invoke(app, ["goal", "add-spending", "Dining", "--limit", "lots"])
    assert result.exit_code == 2
    assert "must be a number" in result.output


def test_goal_add_spending_negative_limit(fresh_home: Path) -> None:
    # --limit=-5 (equals form) so the shell/CLI parser can't read -5 as a flag.
    result = runner.invoke(app, ["goal", "add-spending", "Dining", "--limit=-5"])
    assert result.exit_code == 2
    assert "positive" in result.output


def test_goal_add_spending_duplicate_name(fresh_home: Path) -> None:
    first = runner.invoke(app, ["goal", "add-spending", "Dining", "--limit", "400"])
    assert first.exit_code == 0
    second = runner.invoke(app, ["goal", "add-spending", "dining", "--limit", "400"])
    assert second.exit_code == 1
    assert "already exists" in second.output


# --- add-balance ------------------------------------------------------------


def test_goal_add_balance_happy_path(fresh_home: Path) -> None:
    _seed_account(fresh_home)
    result = runner.invoke(
        app,
        [
            "goal",
            "add-balance",
            "ACT-goal",
            "--target",
            "10000",
            "--direction",
            "at_least",
            "--by",
            "2999-01-01",
            "--name",
            "Emergency fund",
        ],
    )
    assert result.exit_code == 0, result.output
    assert 'Added goal "Emergency fund"' in result.output
    assert "at least 10000 by 2999-01-01" in result.output


def test_goal_add_balance_unknown_account(fresh_home: Path) -> None:
    result = runner.invoke(app, ["goal", "add-balance", "nope", "--target", "100"])
    assert result.exit_code == 1
    assert "account not found" in result.output


def test_goal_add_balance_bad_direction(fresh_home: Path) -> None:
    _seed_account(fresh_home)
    result = runner.invoke(
        app,
        ["goal", "add-balance", "ACT-goal", "--target", "100", "--direction", "exactly"],
    )
    assert result.exit_code == 2
    assert "at_least" in result.output


def test_goal_add_balance_past_by_date(fresh_home: Path) -> None:
    _seed_account(fresh_home)
    result = runner.invoke(
        app,
        ["goal", "add-balance", "ACT-goal", "--target", "100", "--by", "2000-01-01"],
    )
    assert result.exit_code == 2
    assert "future" in result.output


# --- list -------------------------------------------------------------------


def test_goal_list_empty(fresh_home: Path) -> None:
    result = runner.invoke(app, ["goal", "list"])
    assert result.exit_code == 0
    assert "No goals yet" in result.output


def test_goal_list_shows_progress_and_status(fresh_home: Path) -> None:
    _seed_account(fresh_home)
    _seed_current_month_spend(fresh_home, amount="-450.00")
    add = runner.invoke(app, ["goal", "add-spending", "Dining", "--limit", "400"])
    assert add.exit_code == 0
    result = runner.invoke(app, ["goal", "list"])
    assert result.exit_code == 0, result.output
    assert "over" in result.output
    assert "450.00 of 400" in result.output
    # DECIMAL(18,2) round-trip renders scale-2: "400.00".
    assert "Dining under 400.00 per month" in result.output


def test_goal_list_balance_goal_met(fresh_home: Path) -> None:
    _seed_account(fresh_home)  # balance 6500
    add = runner.invoke(
        app,
        ["goal", "add-balance", "ACT-goal", "--target", "5000", "--name", "Floor"],
    )
    assert add.exit_code == 0
    result = runner.invoke(app, ["goal", "list"])
    assert result.exit_code == 0, result.output
    assert "met" in result.output
    assert "6500.00 of 5000" in result.output


# --- remove -----------------------------------------------------------------


def test_goal_remove_with_yes(fresh_home: Path) -> None:
    add = runner.invoke(app, ["goal", "add-spending", "Dining", "--limit", "400"])
    assert add.exit_code == 0
    result = runner.invoke(app, ["goal", "remove", "1", "--yes"])
    assert result.exit_code == 0, result.output
    assert "Removed goal 1" in result.output
    listing = runner.invoke(app, ["goal", "list"])
    assert "No goals yet" in listing.output


def test_goal_remove_confirmation_prompt(fresh_home: Path) -> None:
    add = runner.invoke(app, ["goal", "add-spending", "Dining", "--limit", "400"])
    assert add.exit_code == 0
    declined = runner.invoke(app, ["goal", "remove", "1"], input="n\n")
    assert declined.exit_code == 1
    assert "Aborted" in declined.output
    accepted = runner.invoke(app, ["goal", "remove", "1"], input="y\n")
    assert accepted.exit_code == 0
    assert "Removed goal 1" in accepted.output


def test_goal_remove_unknown(fresh_home: Path) -> None:
    result = runner.invoke(app, ["goal", "remove", "99", "--yes"])
    assert result.exit_code == 1
    assert "goal not found: 99" in result.output


# --- post-sync breach lines ---------------------------------------------------


def test_sync_prints_goal_breach_lines(fresh_home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """After a successful sync, breached goals print as yellow `goal:`
    lines. collect() is stubbed — this pins the CLI wiring, not the
    SimpleFIN fetch."""
    (fresh_home / "config.json").write_text(
        json.dumps(
            {
                "access_url": "https://user:pass@bridge.example/simplefin",
                "backend": "duckdb",
                "db_filename": "data.duckdb",
            }
        ),
        encoding="utf-8",
    )
    _seed_account(fresh_home)
    _seed_current_month_spend(fresh_home, amount="-450.00")
    add = runner.invoke(app, ["goal", "add-spending", "Dining", "--limit", "400"])
    assert add.exit_code == 0

    def fake_collect(store: object, client: object) -> SyncRun:
        now = datetime.now(tz=UTC)
        return SyncRun(started_at=now, finished_at=now)

    monkeypatch.setattr("goetta_finance.cli.collect", fake_collect)
    result = runner.invoke(app, ["sync"])
    assert result.exit_code == 0, result.output
    assert "Synced:" in result.output
    assert "goal:" in result.output
    assert "over" in result.output
    assert "450.00" in result.output


def test_sync_no_breach_prints_no_goal_lines(
    fresh_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (fresh_home / "config.json").write_text(
        json.dumps(
            {
                "access_url": "https://user:pass@bridge.example/simplefin",
                "backend": "duckdb",
                "db_filename": "data.duckdb",
            }
        ),
        encoding="utf-8",
    )
    _seed_account(fresh_home)
    add = runner.invoke(app, ["goal", "add-spending", "Dining", "--limit", "400"])
    assert add.exit_code == 0

    def fake_collect(store: object, client: object) -> SyncRun:
        now = datetime.now(tz=UTC)
        return SyncRun(started_at=now, finished_at=now)

    monkeypatch.setattr("goetta_finance.cli.collect", fake_collect)
    result = runner.invoke(app, ["sync"])
    assert result.exit_code == 0, result.output
    assert "goal:" not in result.output
