"""CLI tests for `goetta-finance import transactions|balances`."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from typer.testing import CliRunner

from goetta_finance.cli import app
from goetta_finance.importer import BALANCES_HEADER, TRANSACTIONS_HEADER
from goetta_finance.models import Account, Transaction
from goetta_finance.store.duckdb_store import DuckDBStore

runner = CliRunner()

ACCOUNT_ID = "ACT-test-checking"
TXN_HEADER_LINE = ",".join(TRANSACTIONS_HEADER)
BAL_HEADER_LINE = ",".join(BALANCES_HEADER)


@pytest.fixture
def fresh_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Initialize a fresh GOETTA_FINANCE_HOME with a migrated DuckDB and one account."""
    monkeypatch.setenv("GOETTA_FINANCE_HOME", str(tmp_path))
    store = DuckDBStore(tmp_path / "data.duckdb")
    store.init()
    store.upsert_accounts(
        [
            Account(
                id=ACCOUNT_ID,
                org_name="Demo Bank",
                name="Checking 1234",
                balance=Decimal("100.00"),
                balance_date=datetime(2026, 7, 1, tzinfo=UTC),
            )
        ]
    )
    store.close()
    return tmp_path


def _open(home: Path) -> DuckDBStore:
    return DuckDBStore(home / "data.duckdb")


def _seed_feed_transaction(home: Path, posted: datetime, txn_id: str = "ACT-TXN-1") -> None:
    store = _open(home)
    try:
        store.upsert_transactions(
            [
                Transaction(
                    id=txn_id,
                    account_id=ACCOUNT_ID,
                    posted=posted,
                    amount=Decimal("-5.00"),
                    description="Synced Row",
                )
            ]
        )
    finally:
        store.close()


def _txn_count(home: Path, prefix: str | None = None) -> int:
    store = _open(home)
    try:
        sql = "SELECT COUNT(*) AS n FROM transactions"
        if prefix:
            rows = store.query_sql(sql + " WHERE id LIKE ?", [f"{prefix}%"])
        else:
            rows = store.query_sql(sql)
        return int(rows[0]["n"])
    finally:
        store.close()


def _write_txn_csv(home: Path, *rows: str) -> Path:
    path = home / "import.csv"
    path.write_text("\n".join([TXN_HEADER_LINE, *rows]) + "\n", encoding="utf-8")
    return path


def _write_bal_csv(home: Path, *rows: str) -> Path:
    path = home / "balances.csv"
    path.write_text("\n".join([BAL_HEADER_LINE, *rows]) + "\n", encoding="utf-8")
    return path


def _invoke(*args: str) -> object:
    return runner.invoke(app, list(args))


def test_import_transactions_end_to_end(fresh_home: Path) -> None:
    csv_path = _write_txn_csv(
        fresh_home,
        "2019-07-15,-12.34,Debit Purchase VISA,2019-07-13,1000000001,card 9999,stmt.pdf",
        "2019-07-26,1250.00,Deposit,,,,",
    )
    result = runner.invoke(app, ["import", "transactions", str(csv_path), "--account", ACCOUNT_ID])
    assert result.exit_code == 0, result.output
    assert "Imported 2 transactions (new: 2, updated: 0)." in result.output

    store = _open(fresh_home)
    try:
        txns = store.get_transactions(account_id=ACCOUNT_ID)
    finally:
        store.close()
    assert len(txns) == 2
    imported = {t.id: t for t in txns}
    assert all(tid.startswith("IMP-") for tid in imported)
    debit = next(t for t in txns if t.amount == Decimal("-12.34"))
    assert debit.pending is False
    assert debit.posted == datetime(2019, 7, 15, 12, 0, 0, tzinfo=UTC)
    assert debit.extra["source"] == "statement_import"
    assert debit.extra["source_file"] == "stmt.pdf"
    assert debit.memo == "card 9999"


def test_import_transactions_idempotent(fresh_home: Path) -> None:
    csv_path = _write_txn_csv(fresh_home, "2019-07-15,-12.34,Debit Purchase VISA,,,,")
    first = runner.invoke(app, ["import", "transactions", str(csv_path), "--account", ACCOUNT_ID])
    assert first.exit_code == 0, first.output
    second = runner.invoke(app, ["import", "transactions", str(csv_path), "--account", ACCOUNT_ID])
    assert second.exit_code == 0, second.output
    assert "(new: 0, update: 1)" in second.output
    assert "Imported 1 transactions (new: 0, updated: 1)." in second.output
    assert _txn_count(fresh_home) == 1


def test_import_skips_overlap_and_echoes_cutoff(fresh_home: Path) -> None:
    _seed_feed_transaction(fresh_home, datetime(2026, 2, 17, 12, 0, tzinfo=UTC))
    csv_path = _write_txn_csv(
        fresh_home,
        "2026-02-16,-1.00,Before Cutoff,,,,",
        "2026-02-17,-2.00,On Cutoff Date,,,,",
        "2026-02-18,-3.00,After Cutoff,,,,",
    )
    result = runner.invoke(app, ["import", "transactions", str(csv_path), "--account", ACCOUNT_ID])
    assert result.exit_code == 0, result.output
    assert "Cutoff: 2026-02-17 (earliest synced transaction" in result.output
    assert "skipped (overlap): 2" in result.output
    assert _txn_count(fresh_home, "IMP-") == 1


def test_import_rerun_does_not_move_cutoff(fresh_home: Path) -> None:
    _seed_feed_transaction(fresh_home, datetime(2026, 2, 17, 12, 0, tzinfo=UTC))
    old_csv = _write_txn_csv(fresh_home, "2019-07-15,-12.34,Old Historical Row,,,,")
    first = runner.invoke(app, ["import", "transactions", str(old_csv), "--account", ACCOUNT_ID])
    assert first.exit_code == 0, first.output
    # Imported IMP- rows are now the oldest in the account; the cutoff must
    # still derive from the synced (non-IMP) row.
    result = runner.invoke(
        app, ["import", "transactions", str(old_csv), "--account", ACCOUNT_ID, "--dry-run"]
    )
    assert result.exit_code == 0, result.output
    assert "Cutoff: 2026-02-17" in result.output


def test_import_before_flag_overrides_cutoff(fresh_home: Path) -> None:
    csv_path = _write_txn_csv(
        fresh_home,
        "2020-01-01,-1.00,Kept,,,,",
        "2021-01-01,-2.00,Dropped,,,,",
    )
    result = runner.invoke(
        app,
        [
            "import",
            "transactions",
            str(csv_path),
            "--account",
            ACCOUNT_ID,
            "--before",
            "2020-06-01",
        ],
    )
    assert result.exit_code == 0, result.output
    assert "Cutoff: 2020-06-01 (--before" in result.output
    assert _txn_count(fresh_home, "IMP-") == 1


def test_import_allow_overlap_imports_everything(fresh_home: Path) -> None:
    _seed_feed_transaction(fresh_home, datetime(2026, 2, 17, 12, 0, tzinfo=UTC))
    csv_path = _write_txn_csv(fresh_home, "2026-03-01,-1.00,Inside Overlap,,,,")
    result = runner.invoke(
        app,
        ["import", "transactions", str(csv_path), "--account", ACCOUNT_ID, "--allow-overlap"],
    )
    assert result.exit_code == 0, result.output
    assert "Cutoff: none (--allow-overlap" in result.output
    assert _txn_count(fresh_home, "IMP-") == 1


def test_import_before_conflicts_with_allow_overlap(fresh_home: Path) -> None:
    csv_path = _write_txn_csv(fresh_home, "2020-01-01,-1.00,x,,,,")
    result = runner.invoke(
        app,
        [
            "import",
            "transactions",
            str(csv_path),
            "--account",
            ACCOUNT_ID,
            "--before",
            "2020-06-01",
            "--allow-overlap",
        ],
    )
    assert result.exit_code != 0
    assert "mutually exclusive" in result.output


def test_import_dry_run_writes_nothing_and_prints_plan(fresh_home: Path) -> None:
    csv_path = _write_txn_csv(
        fresh_home,
        "2019-07-15,-12.34,x,,,,",
        "2020-03-01,25.00,y,,,,",
    )
    result = runner.invoke(
        app, ["import", "transactions", str(csv_path), "--account", ACCOUNT_ID, "--dry-run"]
    )
    assert result.exit_code == 0, result.output
    assert "to import: 2 (new: 2, update: 0)" in result.output
    assert "amount sum: 12.66" in result.output
    assert "per year: 2019: 1  2020: 1" in result.output
    assert "Dry run: nothing written." in result.output
    assert _txn_count(fresh_home) == 0


def test_import_unknown_account_friendly_error(fresh_home: Path) -> None:
    csv_path = _write_txn_csv(fresh_home, "2019-07-15,-1.00,x,,,,")
    result = runner.invoke(app, ["import", "transactions", str(csv_path), "--account", "ACT-nope"])
    assert result.exit_code == 1
    assert "Unknown account id: ACT-nope" in result.output
    assert ACCOUNT_ID in result.output  # known accounts are listed


def test_import_invalid_row_aborts_and_writes_nothing(fresh_home: Path) -> None:
    csv_path = _write_txn_csv(
        fresh_home,
        "2019-07-15,-1.00,ok,,,,",
        "2019-07-16,bogus,bad,,,,",
    )
    result = runner.invoke(app, ["import", "transactions", str(csv_path), "--account", ACCOUNT_ID])
    assert result.exit_code == 1
    assert "row 3" in result.output
    assert _txn_count(fresh_home) == 0


def test_import_balances_end_to_end_eod_timestamp(fresh_home: Path) -> None:
    csv_path = _write_bal_csv(
        fresh_home,
        "2019-07-15,1060.41,stmt.pdf",
        "2019-07-16,-77.63,stmt.pdf",
    )
    result = runner.invoke(app, ["import", "balances", str(csv_path), "--account", ACCOUNT_ID])
    assert result.exit_code == 0, result.output
    assert "Recorded 2 snapshot(s) (0 already existed)." in result.output

    store = _open(fresh_home)
    try:
        history = store.get_balance_history(ACCOUNT_ID, datetime(2019, 1, 1, tzinfo=UTC))
    finally:
        store.close()
    imported = [s for s in history if s.timestamp.time().second == 59]
    assert len(imported) == 2
    assert imported[0].timestamp == datetime(2019, 7, 15, 23, 59, 59, tzinfo=UTC)
    assert imported[0].balance == Decimal("1060.41")


def test_import_balances_rerun_noop(fresh_home: Path) -> None:
    csv_path = _write_bal_csv(fresh_home, "2019-07-15,1060.41,")
    first = runner.invoke(app, ["import", "balances", str(csv_path), "--account", ACCOUNT_ID])
    assert first.exit_code == 0, first.output
    second = runner.invoke(app, ["import", "balances", str(csv_path), "--account", ACCOUNT_ID])
    assert second.exit_code == 0, second.output
    assert "Recorded 0 snapshot(s) (1 already existed)." in second.output


def test_import_balances_dry_run(fresh_home: Path) -> None:
    csv_path = _write_bal_csv(fresh_home, "2019-07-15,1060.41,")
    result = runner.invoke(
        app, ["import", "balances", str(csv_path), "--account", ACCOUNT_ID, "--dry-run"]
    )
    assert result.exit_code == 0, result.output
    assert "date range: 2019-07-15 .. 2019-07-15" in result.output
    assert "Dry run: nothing written." in result.output
    store = _open(fresh_home)
    try:
        history = store.get_balance_history(ACCOUNT_ID, datetime(2019, 1, 1, tzinfo=UTC))
    finally:
        store.close()
    assert all(s.timestamp.year != 2019 for s in history)


def test_import_missing_db_exits_1(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GOETTA_FINANCE_HOME", str(tmp_path))
    csv_path = tmp_path / "import.csv"
    csv_path.write_text(TXN_HEADER_LINE + "\n2019-07-15,-1.00,x,,,,\n", encoding="utf-8")
    result = runner.invoke(app, ["import", "transactions", str(csv_path), "--account", ACCOUNT_ID])
    assert result.exit_code == 1
    assert "No DuckDB store" in result.output
