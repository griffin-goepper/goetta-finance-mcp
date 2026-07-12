"""Unit tests for the pure normalized-CSV import logic in importer.py."""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from goetta_finance.errors import CsvImportError
from goetta_finance.importer import (
    BALANCES_HEADER,
    IMPORT_ID_PREFIX,
    TRANSACTIONS_HEADER,
    BalanceRow,
    ImportRow,
    build_snapshots,
    build_transactions,
    derive_import_id,
    plan_balance_import,
    plan_import,
    read_balances_csv,
    read_transactions_csv,
)

TXN_HEADER_LINE = ",".join(TRANSACTIONS_HEADER)
BAL_HEADER_LINE = ",".join(BALANCES_HEADER)
TODAY = date(2026, 7, 12)


def _write(tmp_path: Path, content: str, name: str = "data.csv") -> Path:
    path = tmp_path / name
    path.write_text(content, encoding="utf-8")
    return path


def _txn_csv(tmp_path: Path, *rows: str) -> Path:
    return _write(tmp_path, "\n".join([TXN_HEADER_LINE, *rows]) + "\n")


def _bal_csv(tmp_path: Path, *rows: str) -> Path:
    return _write(tmp_path, "\n".join([BAL_HEADER_LINE, *rows]) + "\n")


def _row(**overrides: object) -> ImportRow:
    base: dict[str, object] = {
        "posted": date(2019, 7, 15),
        "amount": Decimal("-12.34"),
        "description": "Debit Purchase VISA Coffee Shop",
    }
    base.update(overrides)
    return ImportRow.model_validate(base)


# --- read_transactions_csv ---------------------------------------------------


def test_read_transactions_csv_happy_path(tmp_path: Path) -> None:
    path = _txn_csv(
        tmp_path,
        "2019-07-15,-12.34,Debit Purchase VISA,2019-07-13,1000000001,card 9999,stmt.pdf",
        "2019-07-26,1250.00,Deposit,,,,",
    )
    rows = read_transactions_csv(path, today=TODAY)
    assert len(rows) == 2
    assert rows[0].posted == date(2019, 7, 15)
    assert rows[0].amount == Decimal("-12.34")
    assert rows[0].transacted_at == date(2019, 7, 13)
    assert rows[0].ref_number == "1000000001"
    assert rows[0].memo == "card 9999"
    assert rows[0].source_file == "stmt.pdf"
    assert rows[1].transacted_at is None
    assert rows[1].ref_number is None
    assert rows[1].amount == Decimal("1250.00")


def test_read_csv_rejects_wrong_header(tmp_path: Path) -> None:
    # Missing column.
    path = _write(tmp_path, "posted,amount,description\n2019-07-15,-1.00,x\n")
    with pytest.raises(CsvImportError, match="header must be exactly"):
        read_transactions_csv(path, today=TODAY)
    # Reordered columns.
    reordered = "amount,posted,description,transacted_at,ref_number,memo,source_file"
    path = _write(tmp_path, f"{reordered}\n-1.00,2019-07-15,x,,,,\n")
    with pytest.raises(CsvImportError, match="header must be exactly"):
        read_transactions_csv(path, today=TODAY)
    # Extra column.
    path = _write(tmp_path, TXN_HEADER_LINE + ",extra\n2019-07-15,-1.00,x,,,,,\n")
    with pytest.raises(CsvImportError, match="header must be exactly"):
        read_transactions_csv(path, today=TODAY)


def test_read_csv_rejects_bad_date(tmp_path: Path) -> None:
    path = _txn_csv(tmp_path, "07/15/2019,-1.00,x,,,,")
    with pytest.raises(CsvImportError, match="posted must be YYYY-MM-DD"):
        read_transactions_csv(path, today=TODAY)


def test_read_csv_rejects_future_posted(tmp_path: Path) -> None:
    path = _txn_csv(tmp_path, "2026-07-20,-1.00,x,,,,")
    with pytest.raises(CsvImportError, match="outside sane range"):
        read_transactions_csv(path, today=TODAY)


def test_read_csv_rejects_nan_infinity_amount(tmp_path: Path) -> None:
    for bad in ("NaN", "Infinity", "-Infinity"):
        path = _txn_csv(tmp_path, f"2019-07-15,{bad},x,,,,")
        with pytest.raises(CsvImportError, match="amount must be"):
            read_transactions_csv(path, today=TODAY)


def test_read_csv_rejects_subcent_amount(tmp_path: Path) -> None:
    path = _txn_csv(tmp_path, "2019-07-15,-12.341,x,,,,")
    with pytest.raises(CsvImportError, match="sub-cent"):
        read_transactions_csv(path, today=TODAY)


def test_read_csv_rejects_zero_amount(tmp_path: Path) -> None:
    path = _txn_csv(tmp_path, "2019-07-15,0.00,x,,,,")
    with pytest.raises(CsvImportError, match="non-zero"):
        read_transactions_csv(path, today=TODAY)


def test_read_csv_rejects_empty_description(tmp_path: Path) -> None:
    path = _txn_csv(tmp_path, "2019-07-15,-1.00,   ,,,,")
    with pytest.raises(CsvImportError, match="description must be non-empty"):
        read_transactions_csv(path, today=TODAY)


def test_read_csv_collapses_description_whitespace(tmp_path: Path) -> None:
    path = _txn_csv(tmp_path, '2019-07-15,-1.00,"  Debit   Purchase\tVISA  ",,,,')
    rows = read_transactions_csv(path, today=TODAY)
    assert rows[0].description == "Debit Purchase VISA"


def test_read_csv_rejects_transacted_after_posted(tmp_path: Path) -> None:
    path = _txn_csv(tmp_path, "2019-07-15,-1.00,x,2019-07-16,,,")
    with pytest.raises(CsvImportError, match="is after"):
        read_transactions_csv(path, today=TODAY)


def test_read_csv_error_carries_row_number(tmp_path: Path) -> None:
    path = _txn_csv(
        tmp_path,
        "2019-07-15,-1.00,ok,,,,",
        "2019-07-16,bogus,x,,,,",
    )
    with pytest.raises(CsvImportError, match="row 3"):
        read_transactions_csv(path, today=TODAY)


def test_read_csv_accepts_utf8_bom(tmp_path: Path) -> None:
    path = tmp_path / "bom.csv"
    path.write_text(
        TXN_HEADER_LINE + "\n2019-07-15,-1.00,x,,,,\n",
        encoding="utf-8-sig",
    )
    rows = read_transactions_csv(path, today=TODAY)
    assert len(rows) == 1


def test_read_csv_rejects_empty_file(tmp_path: Path) -> None:
    path = _write(tmp_path, "")
    with pytest.raises(CsvImportError, match="file is empty"):
        read_transactions_csv(path, today=TODAY)
    path = _write(tmp_path, TXN_HEADER_LINE + "\n")
    with pytest.raises(CsvImportError, match="no data rows"):
        read_transactions_csv(path, today=TODAY)


# --- derive_import_id --------------------------------------------------------


def test_derive_import_id_deterministic_and_prefixed() -> None:
    row = _row()
    id_a = derive_import_id("ACT-1", row, 0)
    id_b = derive_import_id("ACT-1", row, 0)
    assert id_a == id_b
    assert id_a.startswith(IMPORT_ID_PREFIX)
    assert len(id_a) == len(IMPORT_ID_PREFIX) + 24
    # Different account, occurrence, or field -> different id.
    assert derive_import_id("ACT-2", row, 0) != id_a
    assert derive_import_id("ACT-1", row, 1) != id_a
    assert derive_import_id("ACT-1", _row(amount=Decimal("-10.60")), 0) != id_a


def test_derive_import_id_occurrence_disambiguates_identical_rows() -> None:
    row = _row()
    txns = build_transactions("ACT-1", [row, row, row])
    assert len({t.id for t in txns}) == 3


def test_derive_import_id_stable_across_amount_formatting() -> None:
    assert derive_import_id("ACT-1", _row(amount=Decimal("-5.1")), 0) == derive_import_id(
        "ACT-1", _row(amount=Decimal("-5.10")), 0
    )


# --- build_transactions ------------------------------------------------------


def test_build_transactions_noon_utc_pending_false_extra_provenance() -> None:
    row = _row(
        transacted_at=date(2019, 7, 13),
        ref_number="1000000001",
        memo="card 9999",
        source_file="stmt.pdf",
    )
    (txn,) = build_transactions("ACT-1", [row])
    assert txn.posted == datetime(2019, 7, 15, 12, 0, 0, tzinfo=UTC)
    assert txn.transacted_at == datetime(2019, 7, 13, 12, 0, 0, tzinfo=UTC)
    assert txn.pending is False
    assert txn.payee is None
    assert txn.memo == "card 9999"
    assert txn.extra == {
        "source": "statement_import",
        "source_file": "stmt.pdf",
        "ref_number": "1000000001",
    }


# --- plan_import -------------------------------------------------------------


def test_plan_import_skips_posted_on_or_after_cutoff() -> None:
    txns = build_transactions(
        "ACT-1",
        [
            _row(posted=date(2026, 2, 16)),
            _row(posted=date(2026, 2, 17)),  # exact cutoff date -> skipped
            _row(posted=date(2026, 2, 18)),
        ],
    )
    plan = plan_import(txns, date(2026, 2, 17))
    assert len(plan.to_import) == 1
    assert plan.to_import[0].posted.date() == date(2026, 2, 16)
    assert plan.skipped_overlap == 2


def test_plan_import_none_cutoff_imports_all() -> None:
    txns = build_transactions(
        "ACT-1",
        [_row(posted=date(2019, 7, 15)), _row(posted=date(2026, 7, 1))],
    )
    plan = plan_import(txns, None)
    assert len(plan.to_import) == 2
    assert plan.skipped_overlap == 0
    assert plan.per_year == {2019: 1, 2026: 1}
    assert plan.amount_sum == Decimal("-24.68")


# --- balances ----------------------------------------------------------------


def test_read_balances_csv_happy_path(tmp_path: Path) -> None:
    path = _bal_csv(
        tmp_path,
        "2019-07-15,1060.41,stmt.pdf",
        "2019-07-16,-77.63,",  # liability balances may be negative
        "2019-07-17,0.00,",  # zero balance is legitimate
    )
    rows = read_balances_csv(path, today=TODAY)
    assert [r.balance for r in rows] == [Decimal("1060.41"), Decimal("-77.63"), Decimal("0.00")]
    snaps = build_snapshots("ACT-1", rows)
    assert snaps[0].timestamp == datetime(2019, 7, 15, 23, 59, 59, tzinfo=UTC)


def test_read_balances_rejects_duplicate_dates(tmp_path: Path) -> None:
    path = _bal_csv(tmp_path, "2019-07-15,1.00,", "2019-07-15,2.00,")
    with pytest.raises(CsvImportError, match="duplicate date"):
        read_balances_csv(path, today=TODAY)


def test_plan_balance_import_applies_cutoff() -> None:
    snaps = build_snapshots(
        "ACT-1",
        [
            BalanceRow(observed_on=date(2026, 2, 16), balance=Decimal("1.00")),
            BalanceRow(observed_on=date(2026, 2, 17), balance=Decimal("2.00")),
        ],
    )
    plan = plan_balance_import(snaps, date(2026, 2, 17))
    assert len(plan.to_import) == 1
    assert plan.to_import[0].timestamp.date() == date(2026, 2, 16)
    assert plan.skipped_overlap == 1
