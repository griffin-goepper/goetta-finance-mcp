from __future__ import annotations

import contextlib
import json
import re
from datetime import UTC, datetime
from decimal import Decimal
from importlib.resources import files
from pathlib import Path
from typing import Any

import duckdb

from goetta_finance.errors import StoreError
from goetta_finance.models import (
    Account,
    AccountType,
    BalanceSnapshot,
    SyncResult,
    SyncRun,
    Transaction,
)

# Intentionally excludes ``explain``. ``EXPLAIN ANALYZE`` *executes* its
# inner statement (DuckDB confirms this for COPY (SELECT 1) TO 'file'),
# and ``COPY ... TO`` writes the filesystem — which a read-only DuckDB
# transaction does NOT block. Removing ``explain`` from the prefix
# whitelist is the cheapest way to keep ``EXPLAIN ANALYZE COPY ... TO
# '/tmp/leak.csv'`` out of the engine entirely. See CLAUDE.md ("Don't
# simplify ``sql_query``'s defense in depth") before adding it back.
_READ_ONLY_PREFIXES = ("select", "with", "show", "describe", "desc")
_STMT_SEP = re.compile(r";\s*")
_LINE_COMMENT = re.compile(r"--[^\n]*")
_BLOCK_COMMENT = re.compile(r"/\*.*?\*/", re.DOTALL)


def _strip_comments(sql: str) -> str:
    return _BLOCK_COMMENT.sub("", _LINE_COMMENT.sub("", sql)).strip()


def _to_naive_utc(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt
    return dt.astimezone(UTC).replace(tzinfo=None)


def _from_naive_utc(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def _json_or_none(value: dict[str, Any]) -> str | None:
    if not value:
        return None
    return json.dumps(value, default=str, sort_keys=True)


def _parse_json(value: Any) -> dict[str, Any]:
    if value in (None, ""):
        return {}
    if isinstance(value, dict):
        return value
    parsed = json.loads(value)
    if not isinstance(parsed, dict):
        return {}
    return parsed


class DuckDBStore:
    """DuckDB-backed FinanceStore.

    Timestamps cross the boundary as tz-aware UTC datetimes; on disk they are
    stored as naive UTC in TIMESTAMP columns.

    ``query_sql`` wraps execution in ``BEGIN TRANSACTION READ ONLY`` so any
    write — including ones embedded in ``WITH ... DELETE`` or
    ``EXPLAIN ANALYZE INSERT`` — is refused by DuckDB's storage layer, not
    just by a string-prefix check. See ``query_sql`` and the security note
    in CLAUDE.md.
    """

    def __init__(self, path: Path | str, *, read_only: bool = False) -> None:
        """Open a DuckDB-backed FinanceStore.

        ``read_only=True`` opens DuckDB with ``read_only=True``: callers
        cannot mutate the store and ``init()`` is a no-op (a read-only
        connection cannot apply migrations). Use this for the web
        dashboard and any other read-side surface that should not be
        able to write. The DB file must already exist and be migrated.
        """
        self.path = str(path)
        self.read_only = read_only
        self._conn: duckdb.DuckDBPyConnection | None = None

    @property
    def conn(self) -> duckdb.DuckDBPyConnection:
        if self._conn is None:
            # ``enable_external_access`` is set at connect time and is
            # immutable: DuckDB refuses ``SET enable_external_access = true``
            # at runtime with "Cannot enable external access while database
            # is running." This is the third layer of sql_query's defense in
            # depth — it blocks ``read_csv``/``read_blob``/``COPY ... TO
            # 'file'``/``httpfs`` at the engine level, including inside
            # otherwise-whitelisted SELECT statements. See CLAUDE.md.
            self._conn = duckdb.connect(
                self.path,
                read_only=self.read_only,
                config={"enable_external_access": "false"},
            )
        return self._conn

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    def init(self) -> None:
        if self.read_only:
            # Read-only stores can't apply migrations. Callers (e.g. the web
            # dashboard) are expected to require a pre-migrated DB.
            return
        self.conn.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations ("
            "name TEXT PRIMARY KEY, applied_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP"
            ")"
        )
        applied = {
            row[0] for row in self.conn.execute("SELECT name FROM schema_migrations").fetchall()
        }
        migrations_dir = files("goetta_finance.store.migrations")
        sql_files = sorted(
            entry.name for entry in migrations_dir.iterdir() if entry.name.endswith(".sql")
        )
        for name in sql_files:
            if name in applied:
                continue
            sql_text = migrations_dir.joinpath(name).read_text()
            try:
                self.conn.execute("BEGIN")
                self.conn.execute(sql_text)
                self.conn.execute("INSERT INTO schema_migrations (name) VALUES (?)", [name])
                self.conn.execute("COMMIT")
            except duckdb.Error as exc:
                self.conn.execute("ROLLBACK")
                raise StoreError(f"Migration {name} failed: {exc}") from exc

    def upsert_accounts(self, accounts: list[Account]) -> None:
        if not accounts:
            return
        rows = [
            (
                a.id,
                a.org_id,
                a.org_name,
                a.name,
                a.currency,
                a.balance,
                a.available_balance,
                _to_naive_utc(a.balance_date),
                a.type.value if a.type is not None else None,
                _json_or_none(a.extra),
            )
            for a in accounts
        ]
        self.conn.executemany(
            """
            INSERT INTO accounts (
                id, org_id, org_name, name, currency, balance, available_balance,
                balance_date, type, extra, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, now())
            ON CONFLICT (id) DO UPDATE SET
                org_id = excluded.org_id,
                org_name = excluded.org_name,
                name = excluded.name,
                currency = excluded.currency,
                balance = excluded.balance,
                available_balance = excluded.available_balance,
                balance_date = excluded.balance_date,
                type = excluded.type,
                extra = excluded.extra,
                updated_at = now()
            """,
            rows,
        )

    def upsert_transactions(self, txns: list[Transaction]) -> SyncResult:
        if not txns:
            return SyncResult()
        ids = [t.id for t in txns]
        placeholders = ", ".join(["?"] * len(ids))
        existing_rows = self.conn.execute(
            f"SELECT id FROM transactions WHERE id IN ({placeholders})", ids
        ).fetchall()
        existing = {row[0] for row in existing_rows}
        new_count = 0
        updated_count = 0
        rows = []
        for t in txns:
            rows.append(
                (
                    t.id,
                    t.account_id,
                    _to_naive_utc(t.posted),
                    _to_naive_utc(t.transacted_at),
                    t.amount,
                    t.description,
                    t.payee,
                    t.memo,
                    t.pending,
                    _json_or_none(t.extra),
                )
            )
            if t.id in existing:
                updated_count += 1
            else:
                new_count += 1
        self.conn.executemany(
            """
            INSERT INTO transactions (
                id, account_id, posted, transacted_at, amount, description,
                payee, memo, pending, extra
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (id) DO UPDATE SET
                account_id = excluded.account_id,
                posted = excluded.posted,
                transacted_at = excluded.transacted_at,
                amount = excluded.amount,
                description = excluded.description,
                payee = excluded.payee,
                memo = excluded.memo,
                pending = excluded.pending,
                extra = excluded.extra
            """,
            rows,
        )
        return SyncResult(new=new_count, updated=updated_count)

    def record_balance_snapshot(self, snap: BalanceSnapshot) -> None:
        self.conn.execute(
            """
            INSERT INTO balance_snapshots (account_id, timestamp, balance)
            VALUES (?, ?, ?)
            ON CONFLICT (account_id, timestamp) DO NOTHING
            """,
            [snap.account_id, _to_naive_utc(snap.timestamp), snap.balance],
        )

    def record_sync_run(self, run: SyncRun) -> int:
        result = self.conn.execute(
            """
            INSERT INTO sync_runs (
                started_at, finished_at, accounts_touched,
                transactions_new, transactions_updated, warnings, errors
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            RETURNING id
            """,
            [
                _to_naive_utc(run.started_at),
                _to_naive_utc(run.finished_at),
                run.accounts_touched,
                run.transactions_new,
                run.transactions_updated,
                json.dumps(run.warnings) if run.warnings else None,
                json.dumps(run.errors) if run.errors else None,
            ],
        ).fetchone()
        if result is None:
            raise StoreError("INSERT INTO sync_runs did not return an id")
        return int(result[0])

    def last_sync_time(self) -> datetime | None:
        row = self.conn.execute(
            "SELECT MAX(finished_at) FROM sync_runs WHERE finished_at IS NOT NULL"
        ).fetchone()
        if row is None or row[0] is None:
            return None
        return _from_naive_utc(row[0])

    def get_accounts(self) -> list[Account]:
        rows = self.conn.execute(
            """
            SELECT id, org_id, org_name, name, currency, balance,
                   available_balance, balance_date, type, extra
            FROM accounts
            ORDER BY org_name, name
            """
        ).fetchall()
        return [self._row_to_account(row) for row in rows]

    def get_transactions(
        self,
        *,
        account_id: str | None = None,
        start: datetime | None = None,
        end: datetime | None = None,
        limit: int | None = None,
    ) -> list[Transaction]:
        clauses: list[str] = []
        params: list[Any] = []
        if account_id is not None:
            clauses.append("account_id = ?")
            params.append(account_id)
        if start is not None:
            clauses.append("posted >= ?")
            params.append(_to_naive_utc(start))
        if end is not None:
            clauses.append("posted <= ?")
            params.append(_to_naive_utc(end))
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        limit_clause = f"LIMIT {int(limit)}" if limit is not None else ""
        rows = self.conn.execute(
            f"""
            SELECT id, account_id, posted, transacted_at, amount, description,
                   payee, memo, pending, extra
            FROM transactions
            {where}
            ORDER BY posted DESC
            {limit_clause}
            """,
            params,
        ).fetchall()
        return [self._row_to_transaction(row) for row in rows]

    def get_balance_history(self, account_id: str, since: datetime) -> list[BalanceSnapshot]:
        rows = self.conn.execute(
            """
            SELECT account_id, timestamp, balance
            FROM balance_snapshots
            WHERE account_id = ? AND timestamp >= ?
            ORDER BY timestamp ASC
            """,
            [account_id, _to_naive_utc(since)],
        ).fetchall()
        return [
            BalanceSnapshot(
                account_id=row[0],
                timestamp=_from_naive_utc(row[1]),  # type: ignore[arg-type]
                balance=row[2],
            )
            for row in rows
        ]

    def query_sql(self, sql: str) -> list[dict[str, Any]]:
        """Run a single read-only SQL statement and return rows as dicts.

        Defense in depth:

        1. **Pre-flight whitelist (fast fail).** Strip comments, reject
           anything that isn't a single statement starting with SELECT/WITH/
           EXPLAIN/SHOW/DESCRIBE. Cheaper than a DuckDB round-trip and gives
           a friendlier error than DuckDB's parser errors.
        2. **Read-only transaction (the actual control).** Wrap execution in
           ``BEGIN TRANSACTION READ ONLY``. DuckDB's storage layer refuses
           writes inside it — including ones smuggled through ``WITH cte AS
           (SELECT 1) DELETE FROM ...`` or ``EXPLAIN ANALYZE INSERT ...``,
           both of which pass the whitelist. (A separate ``read_only=True``
           connection would be ideal but DuckDB rejects two handles to the
           same file from the same process when one is read-write.)

        Both layers are intentional. Do not remove the whitelist on the
        grounds that the transaction is sufficient: the friendly error is
        part of the UX for sql_query. Do not remove the read-only
        transaction on the grounds that the whitelist is sufficient: the
        whitelist is permissive for ``WITH`` and ``EXPLAIN``, which are
        legitimate analytical prefixes that can wrap mutating statements.
        """
        cleaned = _strip_comments(sql)
        statements = [s for s in _STMT_SEP.split(cleaned) if s.strip()]
        if len(statements) != 1:
            raise StoreError("query_sql accepts exactly one statement")
        first_token = statements[0].lstrip().split(None, 1)[0].lower()
        if first_token not in _READ_ONLY_PREFIXES:
            raise StoreError(
                f"query_sql is read-only; rejected statement starting with {first_token!r}"
            )
        conn = self.conn
        try:
            conn.execute("BEGIN TRANSACTION READ ONLY")
        except duckdb.Error as exc:
            raise StoreError(f"query_sql could not start a read-only transaction: {exc}") from exc
        try:
            try:
                cur = conn.execute(statements[0])
            except duckdb.Error as exc:
                raise StoreError(f"query_sql rejected by read-only transaction: {exc}") from exc
            columns = [d[0] for d in cur.description] if cur.description else []
            rows = [dict(zip(columns, row, strict=True)) for row in cur.fetchall()]
        finally:
            try:
                conn.execute("COMMIT")
            except duckdb.Error:
                with contextlib.suppress(duckdb.Error):
                    conn.execute("ROLLBACK")
        return rows

    def _row_to_account(self, row: tuple[Any, ...]) -> Account:
        account_type = AccountType(row[8]) if row[8] is not None else None
        return Account(
            id=row[0],
            org_id=row[1],
            org_name=row[2],
            name=row[3],
            currency=row[4],
            balance=Decimal(row[5]),
            available_balance=Decimal(row[6]) if row[6] is not None else None,
            balance_date=_from_naive_utc(row[7]),  # type: ignore[arg-type]
            type=account_type,
            extra=_parse_json(row[9]),
        )

    def _row_to_transaction(self, row: tuple[Any, ...]) -> Transaction:
        return Transaction(
            id=row[0],
            account_id=row[1],
            posted=_from_naive_utc(row[2]),  # type: ignore[arg-type]
            transacted_at=_from_naive_utc(row[3]),
            amount=Decimal(row[4]),
            description=row[5],
            payee=row[6],
            memo=row[7],
            pending=row[8],
            extra=_parse_json(row[9]),
        )
