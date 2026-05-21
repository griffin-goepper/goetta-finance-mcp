from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest
from typer.testing import CliRunner

from goetta_finance.cli import app
from goetta_finance.store.duckdb_store import DuckDBStore

runner = CliRunner()


@pytest.fixture
def fresh_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Initialize a fresh GOETTA_FINANCE_HOME with a migrated empty DuckDB."""
    monkeypatch.setenv("GOETTA_FINANCE_HOME", str(tmp_path))
    store = DuckDBStore(tmp_path / "data.duckdb")
    store.init()
    store.close()
    return tmp_path


def _list_manual_ids(home: Path) -> list[str]:
    store = DuckDBStore(home / "data.duckdb")
    try:
        return [a.id for a in store.get_accounts() if a.is_manual]
    finally:
        store.close()


def test_account_add_minimal_flags(fresh_home: Path) -> None:
    result = runner.invoke(
        app,
        [
            "account",
            "add",
            "--name",
            "Apple Savings",
            "--org",
            "Apple",
            "--type",
            "savings",
            "--balance",
            "30000",
        ],
    )
    assert result.exit_code == 0, result.output
    assert "MANUAL-" in result.output
    assert "Apple Savings" in result.output

    store = DuckDBStore(fresh_home / "data.duckdb")
    try:
        accounts = store.get_accounts()
    finally:
        store.close()
    assert len(accounts) == 1
    acc = accounts[0]
    assert acc.is_manual is True
    assert acc.balance == Decimal("30000")
    assert acc.id.startswith("MANUAL-")


def test_account_add_as_of_backdated(fresh_home: Path) -> None:
    result = runner.invoke(
        app,
        [
            "account",
            "add",
            "--name",
            "HSA",
            "--org",
            "Optum",
            "--type",
            "savings",
            "--balance",
            "2000",
            "--as-of",
            "2026-05-15",
        ],
    )
    assert result.exit_code == 0, result.output

    store = DuckDBStore(fresh_home / "data.duckdb")
    try:
        rows = store.conn.execute(
            "SELECT timestamp FROM balance_snapshots ORDER BY timestamp"
        ).fetchall()
    finally:
        store.close()
    assert len(rows) == 1
    assert rows[0][0].year == 2026 and rows[0][0].month == 5 and rows[0][0].day == 15


def test_account_add_as_of_future_rejected(fresh_home: Path) -> None:
    result = runner.invoke(
        app,
        [
            "account",
            "add",
            "--name",
            "X",
            "--type",
            "savings",
            "--balance",
            "1",
            "--as-of",
            "2099-01-01",
        ],
    )
    assert result.exit_code != 0
    assert "future" in result.output.lower()


def test_account_add_rejects_bad_type(fresh_home: Path) -> None:
    result = runner.invoke(
        app,
        [
            "account",
            "add",
            "--name",
            "X",
            "--type",
            "not-a-real-type",
            "--balance",
            "1",
        ],
    )
    assert result.exit_code != 0
    assert "type" in result.output.lower()


def test_account_list_marks_manual_rows(fresh_home: Path) -> None:
    runner.invoke(
        app,
        [
            "account",
            "add",
            "--name",
            "Apple Savings",
            "--org",
            "Apple",
            "--type",
            "savings",
            "--balance",
            "30000",
        ],
    )
    result = runner.invoke(app, ["account", "list"])
    assert result.exit_code == 0, result.output
    assert "[manual]" in result.output
    assert "Apple Savings" in result.output


def test_account_set_balance_updates_value_and_records_snapshot(
    fresh_home: Path,
) -> None:
    add_result = runner.invoke(
        app,
        [
            "account",
            "add",
            "--name",
            "Robinhood",
            "--org",
            "Robinhood",
            "--type",
            "investment",
            "--balance",
            "10000",
            "--as-of",
            "2026-05-15",
        ],
    )
    assert add_result.exit_code == 0
    account_id = _list_manual_ids(fresh_home)[0]

    update = runner.invoke(
        app, ["account", "set-balance", account_id, "12500", "--as-of", "2026-05-20"]
    )
    assert update.exit_code == 0, update.output

    store = DuckDBStore(fresh_home / "data.duckdb")
    try:
        acc = next(a for a in store.get_accounts() if a.id == account_id)
        assert acc.balance == Decimal("12500")
        snap_rows = store.conn.execute(
            "SELECT balance FROM balance_snapshots WHERE account_id = ? ORDER BY timestamp",
            [account_id],
        ).fetchall()
    finally:
        store.close()
    assert [r[0] for r in snap_rows] == [Decimal("10000"), Decimal("12500")]


def test_account_set_balance_refuses_non_manual(fresh_home: Path) -> None:
    # Seed a non-manual account directly via the store.
    store = DuckDBStore(fresh_home / "data.duckdb")
    try:
        from datetime import UTC, datetime

        from goetta_finance.models import Account, AccountType

        store.upsert_accounts(
            [
                Account(
                    id="ACT-real",
                    org_id=None,
                    org_name="Bank",
                    name="Checking",
                    currency="USD",
                    balance=Decimal("100"),
                    balance_date=datetime(2026, 5, 1, tzinfo=UTC),
                    type=AccountType.CHECKING,
                )
            ]
        )
    finally:
        store.close()
    result = runner.invoke(app, ["account", "set-balance", "ACT-real", "200"])
    assert result.exit_code != 0
    assert "non-manual" in result.output.lower()


def test_account_remove_refuses_non_manual_id_prefix(fresh_home: Path) -> None:
    result = runner.invoke(app, ["account", "remove", "ACT-real"])
    assert result.exit_code != 0
    assert "non-manual" in result.output.lower()


def test_account_remove_without_force_when_snapshots_exist(fresh_home: Path) -> None:
    runner.invoke(
        app,
        [
            "account",
            "add",
            "--name",
            "Apple Savings",
            "--org",
            "Apple",
            "--type",
            "savings",
            "--balance",
            "30000",
        ],
    )
    account_id = _list_manual_ids(fresh_home)[0]
    result = runner.invoke(app, ["account", "remove", account_id])
    assert result.exit_code != 0
    assert "--force" in result.output
    # Account still exists.
    assert _list_manual_ids(fresh_home) == [account_id]


def test_account_remove_force_typed_confirmation_required(fresh_home: Path) -> None:
    runner.invoke(
        app,
        [
            "account",
            "add",
            "--name",
            "Apple Savings",
            "--org",
            "Apple",
            "--type",
            "savings",
            "--balance",
            "30000",
        ],
    )
    account_id = _list_manual_ids(fresh_home)[0]
    # Wrong name: aborted.
    result = runner.invoke(app, ["account", "remove", account_id, "--force"], input="Wrong Name\n")
    assert result.exit_code != 0
    assert "did not match" in result.output.lower()
    assert _list_manual_ids(fresh_home) == [account_id]


def test_account_remove_force_with_correct_confirmation(fresh_home: Path) -> None:
    runner.invoke(
        app,
        [
            "account",
            "add",
            "--name",
            "Apple Savings",
            "--org",
            "Apple",
            "--type",
            "savings",
            "--balance",
            "30000",
        ],
    )
    account_id = _list_manual_ids(fresh_home)[0]
    result = runner.invoke(
        app, ["account", "remove", account_id, "--force"], input="Apple Savings\n"
    )
    assert result.exit_code == 0, result.output
    assert "Removed" in result.output
    assert _list_manual_ids(fresh_home) == []


def test_account_add_liability_flag_persists(fresh_home: Path) -> None:
    result = runner.invoke(
        app,
        [
            "account",
            "add",
            "--name",
            "Federal Student Loans",
            "--org",
            "Dept of Education",
            "--type",
            "loan",
            "--balance",
            "22500",
            "--liability",
        ],
    )
    assert result.exit_code == 0, result.output
    assert "liability" in result.output.lower()

    store = DuckDBStore(fresh_home / "data.duckdb")
    try:
        accounts = store.get_accounts()
    finally:
        store.close()
    assert len(accounts) == 1
    assert accounts[0].is_liability is True
    assert accounts[0].is_manual is True
    assert accounts[0].balance == Decimal("22500")


def test_account_add_no_liability_by_default(fresh_home: Path) -> None:
    runner.invoke(
        app,
        [
            "account",
            "add",
            "--name",
            "Apple Savings",
            "--org",
            "Apple",
            "--type",
            "savings",
            "--balance",
            "30000",
        ],
    )
    store = DuckDBStore(fresh_home / "data.duckdb")
    try:
        accounts = store.get_accounts()
    finally:
        store.close()
    assert accounts[0].is_liability is False


def test_account_set_liability_toggles_on_manual(fresh_home: Path) -> None:
    runner.invoke(
        app,
        [
            "account",
            "add",
            "--name",
            "Apple Savings",
            "--org",
            "Apple",
            "--type",
            "savings",
            "--balance",
            "30000",
        ],
    )
    account_id = _list_manual_ids(fresh_home)[0]

    on = runner.invoke(app, ["account", "set-liability", account_id, "true"])
    assert on.exit_code == 0, on.output
    store = DuckDBStore(fresh_home / "data.duckdb")
    try:
        assert next(a for a in store.get_accounts() if a.id == account_id).is_liability is True
    finally:
        store.close()

    off = runner.invoke(app, ["account", "set-liability", account_id, "no"])
    assert off.exit_code == 0, off.output
    store = DuckDBStore(fresh_home / "data.duckdb")
    try:
        assert next(a for a in store.get_accounts() if a.id == account_id).is_liability is False
    finally:
        store.close()


def test_account_set_liability_works_on_simplefin_account(fresh_home: Path) -> None:
    """set-liability is intentionally not restricted to manual accounts."""
    from datetime import UTC, datetime

    from goetta_finance.models import Account, AccountType

    store = DuckDBStore(fresh_home / "data.duckdb")
    try:
        store.upsert_accounts(
            [
                Account(
                    id="ACT-real-cc",
                    org_name="Amex",
                    name="Gold",
                    balance=Decimal("0"),
                    balance_date=datetime(2026, 5, 1, tzinfo=UTC),
                    type=AccountType.CREDIT,
                )
            ]
        )
    finally:
        store.close()

    result = runner.invoke(app, ["account", "set-liability", "ACT-real-cc", "true"])
    assert result.exit_code == 0, result.output

    store = DuckDBStore(fresh_home / "data.duckdb")
    try:
        assert next(a for a in store.get_accounts() if a.id == "ACT-real-cc").is_liability is True
    finally:
        store.close()


def test_account_set_liability_rejects_bad_boolean(fresh_home: Path) -> None:
    result = runner.invoke(app, ["account", "set-liability", "MANUAL-x", "maybe"])
    assert result.exit_code != 0
    assert "true" in result.output.lower() or "value" in result.output.lower()


def test_account_set_liability_unknown_id_errors(fresh_home: Path) -> None:
    result = runner.invoke(app, ["account", "set-liability", "MANUAL-does-not-exist", "true"])
    assert result.exit_code != 0
    assert "not found" in result.output.lower()


def test_account_remove_force_yes_skips_prompt(fresh_home: Path) -> None:
    runner.invoke(
        app,
        [
            "account",
            "add",
            "--name",
            "Apple Savings",
            "--org",
            "Apple",
            "--type",
            "savings",
            "--balance",
            "30000",
        ],
    )
    account_id = _list_manual_ids(fresh_home)[0]
    result = runner.invoke(app, ["account", "remove", account_id, "--force", "--yes"])
    assert result.exit_code == 0, result.output
    assert _list_manual_ids(fresh_home) == []
