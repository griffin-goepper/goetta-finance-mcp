from __future__ import annotations

import contextlib
import json
import os
import re
import threading
from collections.abc import Sequence
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
    Category,
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

_SQL_TIMEOUT_DEFAULT_SECONDS = 30.0


def _sql_timeout_seconds() -> float:
    """Read ``query_sql`` statement-timeout from environment, default 30s.

    DuckDB has no built-in statement_timeout, so we enforce one via a
    watchdog ``threading.Timer`` in ``query_sql``. Override with
    ``GOETTA_FINANCE_SQL_TIMEOUT_SECONDS``; non-numeric values fall back
    to the default and log a warning.
    """
    raw = os.environ.get("GOETTA_FINANCE_SQL_TIMEOUT_SECONDS")
    if raw is None:
        return _SQL_TIMEOUT_DEFAULT_SECONDS
    try:
        value = float(raw)
    except ValueError:
        return _SQL_TIMEOUT_DEFAULT_SECONDS
    return value if value > 0 else _SQL_TIMEOUT_DEFAULT_SECONDS


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
        # DuckDB's ``DuckDBPyConnection`` keeps a single per-connection
        # "pending query result" slot — concurrent ``execute()`` calls
        # from different threads on the same connection corrupt it
        # ("Attempting to execute an unsuccessful or closed pending query
        # result"). This lock serializes all DB operations on this store.
        # Reentrant so a method can call another store method internally.
        # Held only for the duration of a single DB op (not across network
        # fetches or other I/O), so concurrent MCP reads + a background
        # collect interleave at the DB-op granularity — pause durations
        # are sub-millisecond, not the full collect window.
        self._lock = threading.RLock()

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
            # Resource limits bound the agent-callable ``query_sql`` path
            # against in-database resource exhaustion (huge
            # ``generate_series``, recursive CTEs, etc.). ``memory_limit``
            # caps the per-process memory DuckDB will allocate;
            # ``threads`` caps parallelism so a query can't pin every
            # core. Statement timeout is enforced separately as a
            # watchdog around ``query_sql`` (DuckDB has no built-in
            # statement_timeout). See docs/SECURITY_AUDIT_2026-05.md.
            self._conn = duckdb.connect(
                self.path,
                read_only=self.read_only,
                config={
                    "enable_external_access": "false",
                    "memory_limit": "512MB",
                    "threads": "2",
                },
            )
        return self._conn

    def close(self) -> None:
        with self._lock:
            if self._conn is not None:
                self._conn.close()
                self._conn = None

    def init(self) -> None:
        if self.read_only:
            # Read-only stores can't apply migrations. Callers (e.g. the web
            # dashboard) are expected to require a pre-migrated DB.
            return
        with self._lock:
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
                a.is_manual,
                a.is_liability,
            )
            for a in accounts
        ]
        with self._lock:
            # Defensive: never let a non-manual upsert clobber an existing
            # is_manual=TRUE row. SimpleFIN ids start with ``ACT-`` and manual
            # ids with ``MANUAL-``, so by id-prefix this can't happen today —
            # but if SimpleFIN ever changes its id format and collides, we
            # want the sync to fail loudly, not silently overwrite the user's
            # locally-maintained record. The CLI layer also gates ``add`` so
            # users can't pick a colliding id.
            non_manual_ids = [a.id for a in accounts if not a.is_manual]
            if non_manual_ids:
                placeholders = ", ".join(["?"] * len(non_manual_ids))
                # ruff S608 / bandit B608: ``placeholders`` is ``"?, ?, ?, ..."``
                # — pure parameter markers, no user data. Audited 2026-05.
                collisions = self.conn.execute(
                    f"SELECT id FROM accounts WHERE is_manual = TRUE AND id IN ({placeholders})",  # noqa: S608  # nosec B608
                    non_manual_ids,
                ).fetchall()
                if collisions:
                    collided = ", ".join(row[0] for row in collisions)
                    raise StoreError(
                        f"refusing to upsert non-manual data over manual account(s): {collided}"
                    )
            self.conn.executemany(
                """
                INSERT INTO accounts (
                    id, org_id, org_name, name, currency, balance, available_balance,
                    balance_date, type, extra, is_manual, is_liability, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, now())
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
                    is_manual = excluded.is_manual,
                    is_liability = excluded.is_liability,
                    updated_at = now()
                """,
                rows,
            )

    def set_account_liability(self, account_id: str, is_liability: bool) -> None:
        """Toggle the ``is_liability`` flag on an existing account.

        Works on any account id (SimpleFIN-sourced or manual). Raises
        ``StoreError`` if the account does not exist. The flag is applied
        retroactively to all historical balance_snapshots when net-worth
        aggregations run — see the plan's risk discussion. Caller should
        warn the user if that's not desired.
        """
        with self._lock:
            row = self.conn.execute("SELECT 1 FROM accounts WHERE id = ?", [account_id]).fetchone()
            if row is None:
                raise StoreError(f"account not found: {account_id}")
            self.conn.execute(
                "UPDATE accounts SET is_liability = ?, updated_at = now() WHERE id = ?",
                [is_liability, account_id],
            )

    def delete_account(self, account_id: str, *, cascade_snapshots: bool = False) -> int:
        """Delete a manual account. Refuses non-manual accounts.

        Returns the number of balance_snapshots rows removed (0 if none).

        If the account has any ``balance_snapshots`` rows, the caller must
        pass ``cascade_snapshots=True`` to remove them too. Otherwise this
        raises ``StoreError`` and leaves the database unchanged. Refuses
        non-manual accounts unconditionally — SimpleFIN-sourced accounts
        cannot be deleted through this method.
        """
        with self._lock:
            row = self.conn.execute(
                "SELECT is_manual FROM accounts WHERE id = ?", [account_id]
            ).fetchone()
            if row is None:
                raise StoreError(f"account not found: {account_id}")
            if not row[0]:
                raise StoreError(f"refusing to delete non-manual account: {account_id}")
            count_row = self.conn.execute(
                "SELECT COUNT(*) FROM balance_snapshots WHERE account_id = ?",
                [account_id],
            ).fetchone()
            snapshot_count = int(count_row[0]) if count_row else 0
            if snapshot_count > 0 and not cascade_snapshots:
                raise StoreError(
                    f"account has {snapshot_count} balance snapshots; "
                    "pass cascade_snapshots=True to remove them"
                )
            # DuckDB enforces FK constraints per-statement, so wrapping
            # both deletes in BEGIN/COMMIT raises FK violation on the
            # accounts DELETE even though the snapshots DELETE precedes
            # it in the same transaction. Letting each statement
            # autocommit means the snapshots are gone by the time the
            # accounts DELETE runs. Trade-off: if the accounts DELETE
            # fails after the snapshots DELETE succeeds, we have an
            # orphaned account with no history — acceptable for a
            # single-user local tool since both statements are simple
            # deletes that don't fail in practice and the user can
            # re-run the remove. Atomicity is not load-bearing here.
            try:
                if snapshot_count > 0:
                    self.conn.execute(
                        "DELETE FROM balance_snapshots WHERE account_id = ?",
                        [account_id],
                    )
                self.conn.execute("DELETE FROM accounts WHERE id = ?", [account_id])
            except duckdb.Error as exc:
                raise StoreError(f"delete_account failed: {exc}") from exc
            return snapshot_count

    def upsert_transactions(self, txns: list[Transaction]) -> SyncResult:
        if not txns:
            return SyncResult()
        ids = [t.id for t in txns]
        placeholders = ", ".join(["?"] * len(ids))
        with self._lock:
            # ruff S608 / bandit B608: ``placeholders`` is ``"?, ?, ?, ..."``
            # — pure parameter markers, no user data. Real values bind via
            # ``ids``. Audited 2026-05.
            existing_rows = self.conn.execute(
                f"SELECT id FROM transactions WHERE id IN ({placeholders})",  # noqa: S608  # nosec B608
                ids,
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
        with self._lock:
            self.conn.execute(
                """
                INSERT INTO balance_snapshots (account_id, timestamp, balance)
                VALUES (?, ?, ?)
                ON CONFLICT (account_id, timestamp) DO NOTHING
                """,
                [snap.account_id, _to_naive_utc(snap.timestamp), snap.balance],
            )

    def record_sync_run(self, run: SyncRun) -> int:
        with self._lock:
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
        with self._lock:
            row = self.conn.execute(
                "SELECT MAX(finished_at) FROM sync_runs WHERE finished_at IS NOT NULL"
            ).fetchone()
        if row is None or row[0] is None:
            return None
        return _from_naive_utc(row[0])

    def get_accounts(self) -> list[Account]:
        with self._lock:
            rows = self.conn.execute(
                """
                SELECT id, org_id, org_name, name, currency, balance,
                       available_balance, balance_date, type, extra,
                       is_manual, is_liability
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
        category: str | None = None,
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
        # When category is requested, route through the view so resolution
        # (override > rule > 'Uncategorized') is applied at read time.
        # Otherwise stay on the bare table — the view's CTEs would force
        # evaluation of the rule-matching join on every call, which is
        # wasted work when the caller doesn't care about category.
        source = "transactions_with_category" if category is not None else "transactions"
        if category is not None:
            clauses.append("category = ?")
            params.append(category)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        limit_clause = f"LIMIT {int(limit)}" if limit is not None else ""
        with self._lock:
            # ruff S608 / bandit B608: ``clauses`` is a fixed allow-list of
            # column predicates (``account_id = ?``, ``posted >= ?``,
            # ``posted <= ?``, ``category = ?``); values bind via ``params``.
            # ``source`` is one of two hard-coded identifiers.
            # ``limit_clause`` is ``LIMIT {int(...)}`` so the only
            # interpolated token is a plain integer. Audited 2026-05.
            rows = self.conn.execute(
                f"""
                SELECT id, account_id, posted, transacted_at, amount, description,
                       payee, memo, pending, extra
                FROM {source}
                {where}
                ORDER BY posted DESC
                {limit_clause}
                """,  # noqa: S608  # nosec B608
                params,
            ).fetchall()
        return [self._row_to_transaction(row) for row in rows]

    def get_transactions_with_category(
        self,
        *,
        account_id: str | None = None,
        start: datetime | None = None,
        end: datetime | None = None,
        category: str | None = None,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        """Return transactions joined to their resolved category through
        the ``transactions_with_category`` view. Each row is a dict
        carrying every column of the view (transaction columns plus
        ``category`` and ``category_color``).

        Deliberately a separate method from ``get_transactions``: that one
        returns ``list[Transaction]`` (no category — the pydantic model
        does not carry one, by design), while this one returns dicts
        carrying the resolved category for callers that want both shapes
        at once (the dashboard, the ``get_transactions`` MCP tool).
        """
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
        if category is not None:
            clauses.append("category = ?")
            params.append(category)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        limit_clause = f"LIMIT {int(limit)}" if limit is not None else ""
        with self._lock:
            # ruff S608 / bandit B608: see ``get_transactions``.
            cur = self.conn.execute(
                f"""
                SELECT id, account_id, posted, transacted_at, amount, description,
                       payee, memo, pending, extra, category, category_color
                FROM transactions_with_category
                {where}
                ORDER BY posted DESC
                {limit_clause}
                """,  # noqa: S608  # nosec B608
                params,
            )
            columns = [d[0] for d in cur.description] if cur.description else []
            rows = cur.fetchall()
        out: list[dict[str, Any]] = []
        for row in rows:
            d = dict(zip(columns, row, strict=True))
            d["posted"] = _from_naive_utc(d["posted"])
            d["transacted_at"] = _from_naive_utc(d["transacted_at"])
            d["amount"] = Decimal(d["amount"])
            d["extra"] = _parse_json(d["extra"])
            out.append(d)
        return out

    def get_balance_history(self, account_id: str, since: datetime) -> list[BalanceSnapshot]:
        with self._lock:
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

    def get_categories(self) -> list[Category]:
        with self._lock:
            rows = self.conn.execute(
                "SELECT id, name, display_color, is_default FROM categories ORDER BY name"
            ).fetchall()
        return [
            Category(id=int(r[0]), name=r[1], display_color=r[2], is_default=bool(r[3]))
            for r in rows
        ]

    def category_counts(self) -> list[dict[str, Any]]:
        """Per-category transaction counts resolved through the view.
        Used by ``goetta-finance category list`` to surface "rough counts"
        without an extra round-trip per category. Returns all categories
        (including those with zero matches today) so the user can see the
        full default set.
        """
        with self._lock:
            cur = self.conn.execute(
                """
                SELECT c.name,
                       COUNT(twc.id) AS transaction_count,
                       c.is_default,
                       c.display_color
                FROM categories c
                LEFT JOIN transactions_with_category twc ON twc.category = c.name
                GROUP BY c.name, c.is_default, c.display_color
                ORDER BY transaction_count DESC, c.name ASC
                """
            )
            columns = [d[0] for d in cur.description] if cur.description else []
            rows = cur.fetchall()
        return [dict(zip(columns, row, strict=True)) for row in rows]

    def add_category(self, name: str, display_color: str | None = None) -> Category:
        name = name.strip()
        if not name:
            raise StoreError("category name cannot be empty")
        with self._lock:
            try:
                row = self.conn.execute(
                    """
                    INSERT INTO categories (name, display_color, is_default)
                    VALUES (?, ?, FALSE)
                    RETURNING id, name, display_color, is_default
                    """,
                    [name, display_color],
                ).fetchone()
            except duckdb.Error as exc:
                raise StoreError(f"add_category failed: {exc}") from exc
        if row is None:
            raise StoreError("add_category did not return a row")
        return Category(id=int(row[0]), name=row[1], display_color=row[2], is_default=bool(row[3]))

    def add_rule(
        self,
        category_name: str,
        *,
        match_type: str,
        pattern: str,
        priority: int = 100,
    ) -> int:
        """Insert a categorization rule. Returns the new rule's id.

        ``match_type`` must be ``'contains'`` or ``'regex'`` (enforced
        by the table's CHECK constraint). ``pattern`` is stored as-is;
        the CLI layer is expected to have run the timeout-bounded
        ``re.compile`` validation before calling — this method does NOT
        re-validate. See CLAUDE.md for the security rationale.
        """
        if match_type not in ("contains", "regex"):
            raise StoreError(f"match_type must be 'contains' or 'regex', got {match_type!r}")
        with self._lock:
            cat_row = self.conn.execute(
                "SELECT id FROM categories WHERE name = ?", [category_name]
            ).fetchone()
            if cat_row is None:
                raise StoreError(f"category not found: {category_name}")
            try:
                row = self.conn.execute(
                    """
                    INSERT INTO category_rules
                        (category_id, match_type, pattern, priority, is_default)
                    VALUES (?, ?, ?, ?, FALSE)
                    RETURNING id
                    """,
                    [int(cat_row[0]), match_type, pattern, int(priority)],
                ).fetchone()
            except duckdb.Error as exc:
                raise StoreError(f"add_rule failed: {exc}") from exc
        if row is None:
            raise StoreError("add_rule did not return a row")
        return int(row[0])

    def remove_rule(self, rule_id: int, *, force: bool = False) -> None:
        """Remove a categorization rule. Refuses defaults without ``force``.

        Same shape as ``account remove`` on non-manual accounts: a default
        rule (``is_default = TRUE``) requires explicit ``force=True`` to
        delete. The CLI layer wraps this with a typed-name confirmation
        prompt for additional safety.
        """
        with self._lock:
            row = self.conn.execute(
                "SELECT is_default FROM category_rules WHERE id = ?", [int(rule_id)]
            ).fetchone()
            if row is None:
                raise StoreError(f"rule not found: {rule_id}")
            if bool(row[0]) and not force:
                raise StoreError(f"refusing to remove default rule {rule_id} without force=True")
            self.conn.execute("DELETE FROM category_rules WHERE id = ?", [int(rule_id)])

    def set_transaction_override(self, transaction_id: str, category_name: str) -> None:
        with self._lock:
            cat_row = self.conn.execute(
                "SELECT id FROM categories WHERE name = ?", [category_name]
            ).fetchone()
            if cat_row is None:
                raise StoreError(f"category not found: {category_name}")
            txn_row = self.conn.execute(
                "SELECT 1 FROM transactions WHERE id = ?", [transaction_id]
            ).fetchone()
            if txn_row is None:
                raise StoreError(f"transaction not found: {transaction_id}")
            self.conn.execute(
                """
                INSERT INTO transaction_overrides (transaction_id, category_id, created_at)
                VALUES (?, ?, now())
                ON CONFLICT (transaction_id) DO UPDATE SET
                    category_id = excluded.category_id,
                    created_at = excluded.created_at
                """,
                [transaction_id, int(cat_row[0])],
            )

    def clear_transaction_override(self, transaction_id: str) -> None:
        with self._lock:
            self.conn.execute(
                "DELETE FROM transaction_overrides WHERE transaction_id = ?",
                [transaction_id],
            )

    def query_sql(self, sql: str, params: Sequence[Any] | None = None) -> list[dict[str, Any]]:
        """Run a single read-only SQL statement and return rows as dicts.

        ``params`` is optional positional-parameter binding (DuckDB ``?``
        placeholders). The MCP ``sql_query`` tool calls without params —
        the model can't bind values anyway — but internal callers
        (``web/aggregations.py``) use this to keep date/limit values out
        of the SQL string. Closes the bandit B608 / ruff S608 class of
        finding at the call sites; the read-only transaction wrapper is
        the real safety control regardless.

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
        with self._lock:
            conn = self.conn
            try:
                conn.execute("BEGIN TRANSACTION READ ONLY")
            except duckdb.Error as exc:
                raise StoreError(
                    f"query_sql could not start a read-only transaction: {exc}"
                ) from exc
            # Statement-timeout watchdog: DuckDB has no built-in
            # statement_timeout, so we call ``conn.interrupt()`` from a
            # ``threading.Timer`` if the query runs longer than
            # GOETTA_FINANCE_SQL_TIMEOUT_SECONDS (default 30s). Always
            # cancel the timer in ``finally`` so a fast query doesn't leak
            # a pending interrupt into the next call. See
            # docs/SECURITY_AUDIT_2026-05.md.
            timeout_seconds = _sql_timeout_seconds()
            timer = threading.Timer(timeout_seconds, conn.interrupt)
            timer.daemon = True
            timer.start()
            try:
                try:
                    if params is None:
                        cur = conn.execute(statements[0])
                    else:
                        cur = conn.execute(statements[0], list(params))
                except duckdb.Error as exc:
                    raise StoreError(f"query_sql rejected by read-only transaction: {exc}") from exc
                columns = [d[0] for d in cur.description] if cur.description else []
                rows = [dict(zip(columns, row, strict=True)) for row in cur.fetchall()]
            finally:
                timer.cancel()
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
            is_manual=bool(row[10]),
            is_liability=bool(row[11]),
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
