"""Pure logic for ``goetta-finance import`` — historical data from normalized CSVs.

Bank statements (or any historical export) are converted *outside* this
package into a normalized CSV; this module validates that CSV, derives
deterministic transaction ids, and plans the write so the CLI command in
``cli.py`` stays thin. Typer-free on purpose: everything here is testable
without a CliRunner, mirroring the ``validators.py`` split. It is NOT in
``validators.py`` because that module is the shared CLI/MCP write-surface
gate — importing is a CLI-only offline maintenance operation.

Input is machine-generated, so validation aborts on the first bad row
(``CsvImportError`` carries the 1-based row number): a bad row means the
extractor has a bug, and partially importing a reconciled statement set
would break the balance arithmetic the extractor guaranteed.

Idempotency contract: ids are ``IMP-`` + a sha256 prefix over the row's
identifying fields plus an occurrence index (0-based, file order) that
disambiguates genuinely identical rows. Re-importing the same CSV is a
pure update. The id is stable only while the extractor emits the same
descriptions in the same deterministic order — the CLI echoes the
new/updated split so an id fork (``new > 0`` on a re-run) is loud.
"""

from __future__ import annotations

import csv
import hashlib
import re
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict

from goetta_finance.errors import CsvImportError
from goetta_finance.models import BalanceSnapshot, Transaction
from goetta_finance.store import FinanceStore

IMPORT_ID_PREFIX = "IMP-"
_ID_HASH_LEN = 24  # sha256 hex prefix: 96 bits, collision-negligible at this scale

TRANSACTIONS_HEADER = [
    "posted",
    "amount",
    "description",
    "transacted_at",
    "ref_number",
    "memo",
    "source_file",
]
BALANCES_HEADER = ["date", "balance", "source_file"]

UPSERT_BATCH_SIZE = 5000

_EARLIEST_DATE = date(1990, 1, 1)
_AMOUNT_MAX = Decimal("1000000000")
_DESCRIPTION_MAX_LEN = 500
_CENT = Decimal("0.01")

# Timestamp conventions. Transactions land at noon UTC to match how
# SimpleFIN-sourced rows are stored. Balance snapshots land at 23:59:59
# UTC: semantically an end-of-day balance (after the day's activity), and
# the time-of-day doubles as the provenance fingerprint — the
# balance_snapshots table has no source column, so imported snapshots are
# identifiable (and deletable) via ``timestamp::TIME = '23:59:59'``.
_NOON_UTC = time(12, 0, 0, tzinfo=UTC)
_EOD_UTC = time(23, 59, 59, tzinfo=UTC)

_WHITESPACE_RUN = re.compile(r"\s+")
_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]")


class ImportRow(BaseModel):
    """One validated transaction row from a normalized CSV."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    posted: date
    amount: Decimal
    description: str
    transacted_at: date | None = None
    ref_number: str | None = None
    memo: str | None = None
    source_file: str | None = None


class BalanceRow(BaseModel):
    """One validated balance row from a normalized CSV."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    observed_on: date
    balance: Decimal
    source_file: str | None = None


@dataclass
class ImportPlan:
    to_import: list[Transaction]
    skipped_overlap: int
    cutoff: date | None
    per_year: dict[int, int]
    amount_sum: Decimal


@dataclass
class BalancePlan:
    to_import: list[BalanceSnapshot]
    skipped_overlap: int
    cutoff: date | None


def _row_error(row_num: int, message: str) -> CsvImportError:
    return CsvImportError(f"row {row_num}: {message}")


def _read_csv_rows(path: Path, expected_header: list[str]) -> list[tuple[int, dict[str, str]]]:
    """Read a CSV, enforce the exact header, and return (row_number, cells) pairs.

    ``utf-8-sig`` tolerates the BOM that Excel and PowerShell prepend on
    Windows — a strict utf-8 read would corrupt the first header cell and
    fail the header check with a baffling message. The exact (ordered)
    header match is deliberate: it catches extractor version skew that
    would otherwise silently mis-map columns.
    """
    rows: list[tuple[int, dict[str, str]]] = []
    try:
        with path.open(newline="", encoding="utf-8-sig") as fh:
            reader = csv.reader(fh)
            header = next(reader, None)
            if header is None:
                raise CsvImportError(f"{path.name}: file is empty")
            if header != expected_header:
                raise CsvImportError(
                    f"{path.name}: header must be exactly "
                    f"{','.join(expected_header)!r} (got {','.join(header)!r})"
                )
            for row_num, cells in enumerate(reader, start=2):
                if not cells:  # blank line (e.g. trailing newline)
                    continue
                if len(cells) != len(expected_header):
                    raise _row_error(
                        row_num,
                        f"expected {len(expected_header)} columns, got {len(cells)}",
                    )
                rows.append((row_num, dict(zip(expected_header, cells, strict=True))))
    except OSError as exc:
        raise CsvImportError(f"cannot read {path}: {exc}") from exc
    if not rows:
        raise CsvImportError(f"{path.name}: no data rows")
    return rows


def _parse_date(raw: str, *, row_num: int, field_name: str, today: date) -> date:
    try:
        value = date.fromisoformat(raw.strip())
    except ValueError as exc:
        raise _row_error(row_num, f"{field_name} must be YYYY-MM-DD, got {raw!r}") from exc
    if value < _EARLIEST_DATE or value > today + timedelta(days=1):
        raise _row_error(
            row_num,
            f"{field_name} {value.isoformat()} outside sane range "
            f"[{_EARLIEST_DATE.isoformat()}, today+1d]",
        )
    return value


def _parse_amount(raw: str, *, row_num: int, field_name: str, allow_zero: bool) -> Decimal:
    try:
        value = Decimal(raw.strip())
    except InvalidOperation as exc:
        raise _row_error(row_num, f"{field_name} must be a decimal number, got {raw!r}") from exc
    if not value.is_finite():
        raise _row_error(row_num, f"{field_name} must be finite, got {raw!r}")
    if not allow_zero and value == 0:
        # Statements never carry zero-amount rows; a zero is a tripwire for
        # a whole class of extractor amount-regex failures.
        raise _row_error(row_num, f"{field_name} must be non-zero")
    if value != value.quantize(_CENT):
        # The column is DECIMAL(18,2): DuckDB would round sub-cent values
        # silently and the stored value would diverge from the hashed one.
        raise _row_error(row_num, f"{field_name} has sub-cent precision: {raw!r}")
    if abs(value) > _AMOUNT_MAX:
        raise _row_error(row_num, f"{field_name} exceeds sanity ceiling: {raw!r}")
    return value


def _clean_description(raw: str, *, row_num: int) -> str:
    # Collapse-before-store-and-hash: extractor line-join tweaks (double
    # spaces, tabs) must not fork ids.
    value = _WHITESPACE_RUN.sub(" ", raw).strip()
    if not value:
        raise _row_error(row_num, "description must be non-empty")
    if len(value) > _DESCRIPTION_MAX_LEN:
        raise _row_error(row_num, f"description exceeds {_DESCRIPTION_MAX_LEN} chars")
    if _CONTROL_CHARS.search(value):
        raise _row_error(row_num, "description contains control characters")
    return value


def _clean_optional(raw: str, *, row_num: int, field_name: str) -> str | None:
    value = raw.strip()
    if not value:
        return None
    if _CONTROL_CHARS.search(value):
        raise _row_error(row_num, f"{field_name} contains control characters")
    return value


def read_transactions_csv(path: Path, *, today: date | None = None) -> list[ImportRow]:
    """Parse and validate a normalized transactions CSV. Aborts on first bad row."""
    effective_today = today or datetime.now(tz=UTC).date()
    out: list[ImportRow] = []
    for row_num, cells in _read_csv_rows(path, TRANSACTIONS_HEADER):
        posted = _parse_date(
            cells["posted"], row_num=row_num, field_name="posted", today=effective_today
        )
        amount = _parse_amount(
            cells["amount"], row_num=row_num, field_name="amount", allow_zero=False
        )
        description = _clean_description(cells["description"], row_num=row_num)
        transacted_at: date | None = None
        if cells["transacted_at"].strip():
            transacted_at = _parse_date(
                cells["transacted_at"],
                row_num=row_num,
                field_name="transacted_at",
                today=effective_today,
            )
            if transacted_at > posted:
                # A transaction date after the post date is a year-wrap bug
                # in the extractor, which is exactly what this catches.
                raise _row_error(
                    row_num,
                    f"transacted_at {transacted_at.isoformat()} is after "
                    f"posted {posted.isoformat()}",
                )
        out.append(
            ImportRow(
                posted=posted,
                amount=amount,
                description=description,
                transacted_at=transacted_at,
                ref_number=_clean_optional(
                    cells["ref_number"], row_num=row_num, field_name="ref_number"
                ),
                memo=_clean_optional(cells["memo"], row_num=row_num, field_name="memo"),
                source_file=_clean_optional(
                    cells["source_file"], row_num=row_num, field_name="source_file"
                ),
            )
        )
    return out


def read_balances_csv(path: Path, *, today: date | None = None) -> list[BalanceRow]:
    """Parse and validate a normalized balances CSV. Aborts on first bad row."""
    effective_today = today or datetime.now(tz=UTC).date()
    out: list[BalanceRow] = []
    seen_dates: dict[date, int] = {}
    for row_num, cells in _read_csv_rows(path, BALANCES_HEADER):
        observed_on = _parse_date(
            cells["date"], row_num=row_num, field_name="date", today=effective_today
        )
        if observed_on in seen_dates:
            raise _row_error(
                row_num,
                f"duplicate date {observed_on.isoformat()} "
                f"(first seen at row {seen_dates[observed_on]})",
            )
        seen_dates[observed_on] = row_num
        balance = _parse_amount(
            cells["balance"], row_num=row_num, field_name="balance", allow_zero=True
        )
        out.append(
            BalanceRow(
                observed_on=observed_on,
                balance=balance,
                source_file=_clean_optional(
                    cells["source_file"], row_num=row_num, field_name="source_file"
                ),
            )
        )
    return out


def derive_import_id(account_id: str, row: ImportRow, occurrence: int) -> str:
    """Deterministic id for an imported row.

    The amount is quantized to two decimals before hashing so a future
    extractor emitting ``5.1`` instead of ``5.10`` does not fork ids. The
    ``\\x1f`` joiner cannot appear in any field (control characters are
    rejected at validation).
    """
    key = "\x1f".join(
        [
            account_id,
            row.posted.isoformat(),
            str(row.amount.quantize(_CENT)),
            row.description,
            row.ref_number or "",
            str(occurrence),
        ]
    )
    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()[:_ID_HASH_LEN]
    return f"{IMPORT_ID_PREFIX}{digest}"


def build_transactions(account_id: str, rows: list[ImportRow]) -> list[Transaction]:
    """Turn validated rows into Transaction models with deterministic ids.

    Occurrence indices are assigned in file order per identical
    (posted, amount, description, ref_number) tuple — id stability
    therefore requires the extractor to emit rows in a deterministic
    order across runs.
    """
    occurrence_counts: dict[tuple[date, Decimal, str, str | None], int] = {}
    txns: list[Transaction] = []
    for row in rows:
        key = (row.posted, row.amount, row.description, row.ref_number)
        occurrence = occurrence_counts.get(key, 0)
        occurrence_counts[key] = occurrence + 1
        extra: dict[str, Any] = {"source": "statement_import"}
        if row.source_file:
            extra["source_file"] = row.source_file
        if row.ref_number:
            extra["ref_number"] = row.ref_number
        txns.append(
            Transaction(
                id=derive_import_id(account_id, row, occurrence),
                account_id=account_id,
                posted=datetime.combine(row.posted, _NOON_UTC),
                transacted_at=(
                    datetime.combine(row.transacted_at, _NOON_UTC) if row.transacted_at else None
                ),
                amount=row.amount,
                description=row.description,
                payee=None,
                memo=row.memo,
                pending=False,
                extra=extra,
            )
        )
    if len({t.id for t in txns}) != len(txns):
        raise CsvImportError(
            "internal error: duplicate import ids derived (occurrence indexing bug)"
        )
    return txns


def build_snapshots(account_id: str, rows: list[BalanceRow]) -> list[BalanceSnapshot]:
    return [
        BalanceSnapshot(
            account_id=account_id,
            balance=row.balance,
            timestamp=datetime.combine(row.observed_on, _EOD_UTC),
        )
        for row in rows
    ]


def resolve_cutoff(store: FinanceStore, account_id: str) -> date | None:
    """Earliest posted date among the account's non-imported transactions.

    Imported rows (``IMP-`` ids) are excluded so re-running an import never
    moves the cutoff earlier. ``None`` means the account has no feed rows
    and everything may be imported.
    """
    rows = store.query_sql(
        "SELECT MIN(posted) AS cutoff FROM transactions WHERE account_id = ? AND id NOT LIKE ?",
        [account_id, f"{IMPORT_ID_PREFIX}%"],
    )
    value = rows[0]["cutoff"] if rows else None
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    raise CsvImportError(f"unexpected cutoff type from store: {type(value).__name__}")


def plan_import(txns: list[Transaction], cutoff: date | None) -> ImportPlan:
    """Split rows at the cutoff (date granularity): posted-date >= cutoff is skipped."""
    to_import: list[Transaction] = []
    skipped = 0
    for txn in txns:
        if cutoff is not None and txn.posted.date() >= cutoff:
            skipped += 1
        else:
            to_import.append(txn)
    per_year: dict[int, int] = {}
    amount_sum = Decimal("0")
    for txn in to_import:
        per_year[txn.posted.year] = per_year.get(txn.posted.year, 0) + 1
        amount_sum += txn.amount
    return ImportPlan(
        to_import=to_import,
        skipped_overlap=skipped,
        cutoff=cutoff,
        per_year=dict(sorted(per_year.items())),
        amount_sum=amount_sum,
    )


def plan_balance_import(snaps: list[BalanceSnapshot], cutoff: date | None) -> BalancePlan:
    to_import: list[BalanceSnapshot] = []
    skipped = 0
    for snap in snaps:
        if cutoff is not None and snap.timestamp.date() >= cutoff:
            skipped += 1
        else:
            to_import.append(snap)
    return BalancePlan(to_import=to_import, skipped_overlap=skipped, cutoff=cutoff)
