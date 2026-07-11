from __future__ import annotations

import contextlib
import json
import logging
import os
import re
import threading
from collections.abc import Sequence
from datetime import UTC, date, datetime
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
    Goal,
    GoalDirection,
    GoalKind,
    GoalPeriod,
    SyncResult,
    SyncRun,
    Transaction,
    TransferLink,
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

logger = logging.getLogger(__name__)


# Explicit two-list separation drives the ON CONFLICT SET clause in
# ``upsert_accounts``. Adding a new column? Pick one:
#   - SimpleFIN-sourced and overwritten on every sync → _SIMPLEFIN_SOURCED_COLUMNS
#   - User-controlled and preserved across syncs    → _USER_OWNED_COLUMNS
#
# The SET clause is generated programmatically from _SIMPLEFIN_SOURCED_COLUMNS,
# so adding a user-owned column is a one-line edit to the second tuple and
# correct by construction — the SimpleFIN parser's natural default (False)
# for the new column will not clobber the user's set value on the next sync.
#
# Why this exists: pre-0005 the SET clause enumerated every column including
# ``is_liability`` and ``is_manual``. The SimpleFIN parser always produces
# ``is_liability=False`` (there's no SimpleFIN field for it), so every sync
# silently reset any user-set TRUE back to FALSE. The class-of-bug is pinned
# by ``test_user_owned_flags_survive_sync`` — a new user-controlled flag
# that lands in _SIMPLEFIN_SOURCED_COLUMNS by mistake will also fail it.
# See CLAUDE.md "Things to avoid" for the don't-recreate-this guidance.
_SIMPLEFIN_SOURCED_COLUMNS: tuple[str, ...] = (
    "org_id",
    "org_name",
    "name",
    "currency",
    "balance",
    "available_balance",
    "balance_date",
    "type",
    "extra",
)
_USER_OWNED_COLUMNS: tuple[str, ...] = (
    "is_manual",
    "is_liability",
    "is_hidden",
)
_UPSERT_SET_CLAUSE = (
    ",\n                    ".join(f"{col} = excluded.{col}" for col in _SIMPLEFIN_SOURCED_COLUMNS)
    + ",\n                    updated_at = now()"
)


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


def _normalize_query_param(value: Any) -> Any:
    """Bind tz-aware datetimes as naive UTC, matching on-disk storage.

    A tz-aware datetime param binds as TIMESTAMP WITH TIME ZONE, and
    DuckDB resolves a TIMESTAMP-vs-TIMESTAMPTZ comparison by casting
    the naive *column* through the session (local) time zone — which
    silently shifts every date-window comparison by the machine's UTC
    offset. Measured: a row stored at naive 2026-05-31T23:59:59.999999
    compared ``<=`` tz-aware 2026-05-31T23:59:59.999999+00:00 returns
    FALSE on an America/New_York machine (the column casts to
    2026-06-01T03:59:59Z). Normalizing at the bind boundary keeps
    query windows identical on every machine and matches the
    ``_to_naive_utc`` convention the typed read methods use.
    ``date`` values are not datetimes and pass through untouched.
    """
    if isinstance(value, datetime):
        return _to_naive_utc(value)
    return value


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


def is_database_invalidated(exc: BaseException) -> bool:
    """True when ``exc`` means DuckDB has invalidated the whole in-process
    database (``FatalException``): every subsequent operation on any
    connection to it fails until the process reopens the file.

    A long-lived server that merely logs this becomes a zombie — alive and
    answering, every query a 500 (the 2026-07-06 incident: the daemon
    served errors all morning while the supervisor, which only heals
    process exits, saw a healthy process). Callers owning a process
    lifecycle should treat this as "restart me now". Lives here rather
    than in ``errors.py`` because it is duckdb-backend-specific.
    """
    return isinstance(exc, duckdb.FatalException)


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
                try:
                    self._conn.close()
                except duckdb.FatalException:
                    # An invalidated database refuses even close(); there is
                    # nothing checkpointable left in this process, and the
                    # shutdown paths (CLI ``finally``, daemon fail-fast) must
                    # not die on cleanup.
                    logger.warning("store close skipped: database already invalidated")
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
            applied_any = False
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
                applied_any = True
            if applied_any:
                # Flush freshly applied DDL out of the WAL immediately.
                # DuckDB's WAL replay chokes on CREATE OR REPLACE VIEW
                # entries ("GetDefaultDatabase with no default database
                # set", observed live 2026-07-05 after migration 0009):
                # if the process is force-killed before a natural
                # checkpoint, every subsequent open of the database fails
                # until the WAL is manually moved aside. Checkpointing
                # here closes that window — migration DDL never outlives
                # init() in the WAL.
                self.conn.execute("CHECKPOINT")

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
                a.is_hidden,
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
            # The ON CONFLICT SET clause is generated from
            # _SIMPLEFIN_SOURCED_COLUMNS at module load time — user-owned
            # flags (is_manual, is_liability, is_hidden) are deliberately
            # absent from the SET clause so a sync that re-upserts an
            # account does NOT overwrite the user's set values back to
            # the SimpleFIN parser's natural False default. See the
            # module-level comment for the full rationale.
            self.conn.executemany(
                f"""
                INSERT INTO accounts (
                    id, org_id, org_name, name, currency, balance, available_balance,
                    balance_date, type, extra, is_manual, is_liability, is_hidden, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, now())
                ON CONFLICT (id) DO UPDATE SET
                    {_UPSERT_SET_CLAUSE}
                """,  # noqa: S608  # nosec B608
                rows,
            )

    def set_account_hidden(self, account_id: str, is_hidden: bool) -> None:
        """Toggle the ``is_hidden`` flag on an existing account.

        Works on any account id (SimpleFIN-sourced or manual). Raises
        ``StoreError`` if the account does not exist. The flag is
        preserved across SimpleFIN syncs (the upsert's SET clause
        excludes user-owned columns; see _SIMPLEFIN_SOURCED_COLUMNS
        and _USER_OWNED_COLUMNS at the module top).

        Hidden accounts disappear from default read paths
        (``get_accounts``, ``get_transactions``,
        ``get_transactions_with_category``, the
        ``transactions_with_category`` view's ``account_is_hidden``
        column, net-worth aggregation, spending_by_category).
        ``include_hidden=True`` on the relevant calls opts back in.
        """
        with self._lock:
            row = self.conn.execute("SELECT 1 FROM accounts WHERE id = ?", [account_id]).fetchone()
            if row is None:
                raise StoreError(f"account not found: {account_id}")
            self.conn.execute(
                "UPDATE accounts SET is_hidden = ?, updated_at = now() WHERE id = ?",
                [is_hidden, account_id],
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
            # Goals are user-authored config — never silently cascade
            # them away with the account (the 0007 lesson). Refuse with
            # the fix spelled out.
            goal_row = self.conn.execute(
                "SELECT COUNT(*) FROM goals WHERE account_id = ?", [account_id]
            ).fetchone()
            goal_count = int(goal_row[0]) if goal_row else 0
            if goal_count > 0:
                raise StoreError(
                    f"account has {goal_count} goal(s) referencing it; remove them "
                    "first with `goetta-finance goal remove <id>` "
                    "(see `goetta-finance goal list`)"
                )
            # Transfer links are user-authored config too — same refusal
            # shape as goals. Only account_id can reference a manual
            # account (link creation refuses manual sources), but check
            # both columns so a future relaxation fails loudly here.
            link_row = self.conn.execute(
                "SELECT COUNT(*) FROM transfer_links WHERE account_id = ? OR source_account_id = ?",
                [account_id, account_id],
            ).fetchone()
            link_count = int(link_row[0]) if link_row else 0
            if link_count > 0:
                raise StoreError(
                    f"account has {link_count} transfer link(s) referencing it; remove "
                    "them first with `goetta-finance account unlink <id>` "
                    "(see `goetta-finance account links`)"
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
                # Application-ledger rows are derived bookkeeping (unlike
                # links/goals, which are user config) — clean them up
                # without ceremony. They can exist after an unlink.
                self.conn.execute(
                    "DELETE FROM transfer_link_applications WHERE account_id = ?",
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

    def get_accounts(self, *, include_hidden: bool = False) -> list[Account]:
        """Return accounts. By default filters out hidden accounts.

        Pass ``include_hidden=True`` to see the full list — used by the
        CLI's ``account list`` (so users can find what they've hidden in
        order to unhide it) and the dashboard's footer count of hidden
        accounts. The MCP ``list_accounts`` tool and the dashboard
        Accounts page take the default behavior.
        """
        where = "" if include_hidden else "WHERE COALESCE(is_hidden, FALSE) = FALSE"
        with self._lock:
            rows = self.conn.execute(
                f"""
                SELECT id, org_id, org_name, name, currency, balance,
                       available_balance, balance_date, type, extra,
                       is_manual, is_liability, is_hidden
                FROM accounts
                {where}
                ORDER BY org_name, name
                """  # noqa: S608  # nosec B608
            ).fetchall()
        return [self._row_to_account(row) for row in rows]

    def get_transactions(
        self,
        *,
        account_id: str | None = None,
        start: datetime | None = None,
        end: datetime | None = None,
        category: str | None = None,
        include_hidden: bool = False,
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
        # When category is requested OR we need to filter hidden accounts,
        # route through the view (it carries ``account_is_hidden``). The
        # view's CTEs evaluate the rule-matching join, so we only pay
        # that cost when category info is in scope.
        use_view = category is not None or not include_hidden
        source = "transactions_with_category" if use_view else "transactions"
        if category is not None:
            clauses.append("category = ?")
            params.append(category)
        if not include_hidden:
            # Filter through the view's account_is_hidden column.
            clauses.append("COALESCE(account_is_hidden, FALSE) = FALSE")
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        limit_clause = f"LIMIT {int(limit)}" if limit is not None else ""
        with self._lock:
            # ruff S608 / bandit B608: ``clauses`` is a fixed allow-list of
            # column predicates (``account_id = ?``, ``posted >= ?``,
            # ``posted <= ?``, ``category = ?``, the hidden filter is a
            # string literal); values bind via ``params``. ``source`` is
            # one of two hard-coded identifiers. ``limit_clause`` is
            # ``LIMIT {int(...)}`` so the only interpolated token is a
            # plain integer. Audited 2026-05.
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
        include_hidden: bool = False,
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
        if not include_hidden:
            clauses.append("COALESCE(account_is_hidden, FALSE) = FALSE")
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
                "SELECT id, name, display_color, is_default, is_spending "
                "FROM categories ORDER BY name"
            ).fetchall()
        return [
            Category(
                id=int(r[0]),
                name=r[1],
                display_color=r[2],
                is_default=bool(r[3]),
                is_spending=bool(r[4]),
            )
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

    def add_category(
        self,
        name: str,
        display_color: str | None = None,
        *,
        is_spending: bool = True,
    ) -> Category:
        """Insert a new category. Defaults to is_spending=True. Pass
        ``is_spending=False`` for categories that shouldn't show up in
        the dashboard's "By category" pie or `spending_by_category`
        results (Transfers, Income, payroll deductions, etc.)."""
        name = name.strip()
        if not name:
            raise StoreError("category name cannot be empty")
        with self._lock:
            try:
                row = self.conn.execute(
                    """
                    INSERT INTO categories (name, display_color, is_default, is_spending)
                    VALUES (?, ?, FALSE, ?)
                    RETURNING id, name, display_color, is_default, is_spending
                    """,
                    [name, display_color, is_spending],
                ).fetchone()
            except duckdb.Error as exc:
                raise StoreError(f"add_category failed: {exc}") from exc
        if row is None:
            raise StoreError("add_category did not return a row")
        return Category(
            id=int(row[0]),
            name=row[1],
            display_color=row[2],
            is_default=bool(row[3]),
            is_spending=bool(row[4]),
        )

    def set_category_spending(self, name: str, is_spending: bool) -> None:
        """Toggle the ``is_spending`` flag on an existing category.

        Categories where ``is_spending=FALSE`` are excluded by default
        from ``spending_by_category`` results and the dashboard's
        "Spending by category" pie. ``Transfers`` and ``Income`` are
        non-spending by default (set by migration 0006); the user can
        flip the flag on any other category via this method.
        """
        with self._lock:
            row = self.conn.execute(
                "SELECT 1 FROM categories WHERE lower(name) = lower(?)", [name]
            ).fetchone()
            if row is None:
                raise StoreError(f"category not found: {name}")
            self.conn.execute(
                "UPDATE categories SET is_spending = ? WHERE lower(name) = lower(?)",
                [is_spending, name],
            )

    def add_rule(
        self,
        category_name: str,
        *,
        match_type: str,
        pattern: str,
        priority: int = 100,
        min_amount: Decimal | None = None,
        max_amount: Decimal | None = None,
    ) -> int:
        """Insert a categorization rule. Returns the new rule's id.

        ``match_type`` must be ``'contains'`` or ``'regex'`` (enforced
        by the table's CHECK constraint). ``pattern`` is stored as-is;
        the CLI layer is expected to have run the timeout-bounded
        ``re.compile`` validation before calling — this method does NOT
        re-validate. See CLAUDE.md for the security rationale. The
        optional amount bounds (compared against abs(amount) by the
        view, half-open [min, max)) are likewise expected pre-validated
        by ``validators.validate_rule_amount_bounds``.
        """
        if match_type not in ("contains", "regex"):
            raise StoreError(f"match_type must be 'contains' or 'regex', got {match_type!r}")
        with self._lock:
            cat_row = self.conn.execute(
                "SELECT id FROM categories WHERE lower(name) = lower(?)", [category_name]
            ).fetchone()
            if cat_row is None:
                raise StoreError(f"category not found: {category_name}")
            try:
                row = self.conn.execute(
                    """
                    INSERT INTO category_rules
                        (category_id, match_type, pattern, priority, is_default,
                         min_amount, max_amount)
                    VALUES (?, ?, ?, ?, FALSE, ?, ?)
                    RETURNING id
                    """,
                    [
                        int(cat_row[0]),
                        match_type,
                        pattern,
                        int(priority),
                        min_amount,
                        max_amount,
                    ],
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
                "SELECT id FROM categories WHERE lower(name) = lower(?)", [category_name]
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

    def add_goal(
        self,
        name: str,
        *,
        kind: str,
        amount: Decimal,
        category_name: str | None = None,
        period: str | None = None,
        account_id: str | None = None,
        direction: str | None = None,
        target_date: date | None = None,
    ) -> Goal:
        """Insert a goal (migration 0008). Returns the stored Goal.

        Re-checks the per-kind column shape with friendly errors before
        the table CHECK constraint gets a chance to produce raw SQL
        text. Category lookup is case-insensitive; the "category not
        found: X" message shape is load-bearing (the CLI and MCP
        did-you-mean helpers key off it).
        """
        name = name.strip()
        if not name:
            raise StoreError("goal name cannot be empty")
        if kind == "spending_cap":
            if category_name is None or period is None:
                raise StoreError("spending_cap goals require a category and a period")
            if account_id is not None or direction is not None or target_date is not None:
                raise StoreError(
                    "spending_cap goals do not take account_id, direction, or target_date"
                )
        elif kind == "balance":
            if account_id is None or direction is None:
                raise StoreError("balance goals require an account_id and a direction")
            if category_name is not None or period is not None:
                raise StoreError("balance goals do not take a category or period")
        else:
            raise StoreError(f"kind must be 'spending_cap' or 'balance', got {kind!r}")
        with self._lock:
            dup_row = self.conn.execute(
                "SELECT 1 FROM goals WHERE lower(name) = lower(?)", [name]
            ).fetchone()
            if dup_row is not None:
                raise StoreError(f"goal already exists: {name}")
            category_id: int | None = None
            resolved_category: str | None = None
            if category_name is not None:
                cat_row = self.conn.execute(
                    "SELECT id, name FROM categories WHERE lower(name) = lower(?)",
                    [category_name],
                ).fetchone()
                if cat_row is None:
                    raise StoreError(f"category not found: {category_name}")
                category_id = int(cat_row[0])
                resolved_category = cat_row[1]
            resolved_account: str | None = None
            if account_id is not None:
                acct_row = self.conn.execute(
                    "SELECT name FROM accounts WHERE id = ?", [account_id]
                ).fetchone()
                if acct_row is None:
                    raise StoreError(f"account not found: {account_id}")
                resolved_account = acct_row[0]
            try:
                row = self.conn.execute(
                    """
                    INSERT INTO goals
                        (name, kind, amount, category_id, period,
                         account_id, direction, target_date)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    RETURNING id, created_at
                    """,
                    [name, kind, amount, category_id, period, account_id, direction, target_date],
                ).fetchone()
            except duckdb.Error as exc:
                raise StoreError(f"add_goal failed: {exc}") from exc
        if row is None:
            raise StoreError("add_goal did not return a row")
        created_at = _from_naive_utc(row[1])
        if created_at is None:  # pragma: no cover - NOT NULL column
            raise StoreError("add_goal returned no created_at")
        return Goal(
            id=int(row[0]),
            name=name,
            kind=GoalKind(kind),
            amount=amount,
            category_id=category_id,
            category_name=resolved_category,
            period=GoalPeriod(period) if period is not None else None,
            account_id=account_id,
            account_name=resolved_account,
            direction=GoalDirection(direction) if direction is not None else None,
            target_date=target_date,
            created_at=created_at,
        )

    def list_goals(self) -> list[Goal]:
        """All goals with display names resolved, ordered by name."""
        with self._lock:
            rows = self.conn.execute(
                """
                SELECT g.id, g.name, g.kind, g.amount, g.category_id, c.name,
                       g.period, g.account_id, a.name, g.direction,
                       g.target_date, g.created_at
                FROM goals g
                LEFT JOIN categories c ON c.id = g.category_id
                LEFT JOIN accounts a ON a.id = g.account_id
                ORDER BY g.name
                """
            ).fetchall()
        return [self._row_to_goal(row) for row in rows]

    def remove_goal(self, goal_id: int) -> None:
        with self._lock:
            row = self.conn.execute("SELECT 1 FROM goals WHERE id = ?", [int(goal_id)]).fetchone()
            if row is None:
                raise StoreError(f"goal not found: {goal_id}")
            self.conn.execute("DELETE FROM goals WHERE id = ?", [int(goal_id)])

    @staticmethod
    def _row_to_goal(row: tuple[Any, ...]) -> Goal:
        created_at = _from_naive_utc(row[11])
        if created_at is None:  # pragma: no cover - NOT NULL column
            raise StoreError("goal row has no created_at")
        return Goal(
            id=int(row[0]),
            name=row[1],
            kind=GoalKind(row[2]),
            amount=row[3],
            category_id=int(row[4]) if row[4] is not None else None,
            category_name=row[5],
            period=GoalPeriod(row[6]) if row[6] is not None else None,
            account_id=row[7],
            account_name=row[8],
            direction=GoalDirection(row[9]) if row[9] is not None else None,
            target_date=row[10],
            created_at=created_at,
        )

    def add_transfer_link(
        self,
        account_id: str,
        source_account_id: str,
        *,
        match_type: str,
        pattern: str,
    ) -> TransferLink:
        """Insert a transfer link (migration 0012). Returns the stored link.

        Shape checks with friendly errors: the destination must be a
        manual, non-liability account (liability roll-forward is refused
        in v1 — the stored sign of a manual liability balance is
        unspecified, everything reads it through abs(), so a signed
        delta is ambiguous); the source must be a non-manual account;
        both must share a currency (summing transfers across currencies
        would corrupt the balance). ``pattern`` is stored as-is; the
        surface layer runs ``validators.validate_rule_pattern`` first,
        same contract as ``add_rule``.

        The anchor starts at the destination's ``balance_date``: that
        balance is trusted to include everything posted at or before it.
        """
        if match_type not in ("contains", "regex"):
            raise StoreError(f"match_type must be 'contains' or 'regex', got {match_type!r}")
        with self._lock:
            dest_row = self.conn.execute(
                "SELECT is_manual, is_liability, currency, balance_date FROM accounts WHERE id = ?",
                [account_id],
            ).fetchone()
            if dest_row is None:
                raise StoreError(f"account not found: {account_id}")
            if not dest_row[0]:
                raise StoreError(
                    f"transfer links roll forward manual accounts only; {account_id} is synced "
                    "(its balance already updates on every sync)"
                )
            if dest_row[1]:
                raise StoreError(
                    f"transfer links are not supported on liability accounts yet; track "
                    f"paydown on {account_id} with `account set-balance` true-ups"
                )
            source_row = self.conn.execute(
                "SELECT is_manual, currency FROM accounts WHERE id = ?",
                [source_account_id],
            ).fetchone()
            if source_row is None:
                raise StoreError(f"account not found: {source_account_id}")
            if source_row[0]:
                raise StoreError(
                    f"the source of a transfer link must be a synced account; "
                    f"{source_account_id} is manual and has no transaction feed"
                )
            if dest_row[2] != source_row[1]:
                raise StoreError(
                    f"currency mismatch: {account_id} is {dest_row[2]} but "
                    f"{source_account_id} is {source_row[1]}"
                )
            try:
                row = self.conn.execute(
                    """
                    INSERT INTO transfer_links
                        (account_id, source_account_id, match_type, pattern, anchor)
                    VALUES (?, ?, ?, ?, ?)
                    RETURNING id
                    """,
                    [account_id, source_account_id, match_type, pattern, dest_row[3]],
                ).fetchone()
            except duckdb.Error as exc:
                raise StoreError(f"add_transfer_link failed: {exc}") from exc
        if row is None:
            raise StoreError("add_transfer_link did not return a row")
        links = self.list_transfer_links(account_id=account_id)
        for link in links:
            if link.id == int(row[0]):
                return link
        raise StoreError("add_transfer_link could not read back the inserted row")

    def list_transfer_links(self, *, account_id: str | None = None) -> list[TransferLink]:
        """All transfer links with display names resolved, ordered by id."""
        where = "WHERE l.account_id = ?" if account_id is not None else ""
        params = [account_id] if account_id is not None else []
        with self._lock:
            # ruff S608 / bandit B608: ``where`` is one of two hard-coded
            # strings; the value binds via ``params``. Audited 2026-07.
            rows = self.conn.execute(
                f"""
                SELECT l.id, l.account_id, d.name, l.source_account_id, s.name,
                       l.match_type, l.pattern, l.anchor, l.created_at
                FROM transfer_links l
                LEFT JOIN accounts d ON d.id = l.account_id
                LEFT JOIN accounts s ON s.id = l.source_account_id
                {where}
                ORDER BY l.id
                """,  # noqa: S608  # nosec B608
                params,
            ).fetchall()
        return [self._row_to_transfer_link(row) for row in rows]

    def remove_transfer_link(self, link_id: int) -> None:
        """Remove a transfer link. The applications ledger is kept on
        purpose: already-applied transactions stay applied (the balance
        reflects them), and a later re-link must not double-count them."""
        with self._lock:
            row = self.conn.execute(
                "SELECT 1 FROM transfer_links WHERE id = ?", [int(link_id)]
            ).fetchone()
            if row is None:
                raise StoreError(f"transfer link not found: {link_id}")
            self.conn.execute("DELETE FROM transfer_links WHERE id = ?", [int(link_id)])

    def reset_transfer_link_anchors(self, account_id: str, anchor: datetime) -> int:
        """Re-anchor an account's links after a manual true-up.

        A ``set-balance`` declares the balance as of its ``--as-of``
        moment, absorbing every transfer posted at or before it — so the
        anchors move there, and ledger rows for transactions posted
        *after* it are released to re-apply against the new base (the
        backdated-true-up case). Returns the number of links touched.
        """
        naive = _to_naive_utc(anchor)
        with self._lock:
            rows = self.conn.execute(
                "SELECT id FROM transfer_links WHERE account_id = ?", [account_id]
            ).fetchall()
            if not rows:
                return 0
            self.conn.execute(
                "UPDATE transfer_links SET anchor = ? WHERE account_id = ?",
                [naive, account_id],
            )
            self.conn.execute(
                "DELETE FROM transfer_link_applications WHERE account_id = ? AND posted > ?",
                [account_id, naive],
            )
            return len(rows)

    def eligible_transfer_transactions(self, link: TransferLink) -> list[Transaction]:
        """Matched source transactions not yet applied to the link's account.

        Eligible = on the source account, settled (pending transactions
        wait — their ids and posted times are not stable), posted
        strictly after the anchor, pattern-matched against payee OR
        description, and absent from the applications ledger. Ordered by
        posted so the roll-forward advances chronologically.
        """
        if link.match_type == "contains":
            match_clause = (
                "(lower(t.description) LIKE '%' || lower(?) || '%' "
                "OR lower(COALESCE(t.payee, '')) LIKE '%' || lower(?) || '%')"
            )
        else:
            match_clause = (
                "(regexp_matches(t.description, ?) OR regexp_matches(COALESCE(t.payee, ''), ?))"
            )
        with self._lock:
            # ruff S608 / bandit B608: ``match_clause`` is one of two
            # hard-coded strings; the pattern binds via parameters.
            # Audited 2026-07.
            rows = self.conn.execute(
                f"""
                SELECT t.id, t.account_id, t.posted, t.transacted_at, t.amount,
                       t.description, t.payee, t.memo, t.pending, t.extra
                FROM transactions t
                WHERE t.account_id = ?
                  AND t.pending = FALSE
                  AND t.posted > ?
                  AND {match_clause}
                  AND NOT EXISTS (
                      SELECT 1 FROM transfer_link_applications a
                      WHERE a.transaction_id = t.id AND a.account_id = ?
                  )
                ORDER BY t.posted
                """,  # noqa: S608  # nosec B608
                [
                    link.source_account_id,
                    _to_naive_utc(link.anchor),
                    link.pattern,
                    link.pattern,
                    link.account_id,
                ],
            ).fetchall()
        return [self._row_to_transaction(row) for row in rows]

    def record_transfer_applications(
        self, account_id: str, link_id: int, txns: list[Transaction]
    ) -> None:
        """Ledger the transactions just applied to a manual account.

        ``ON CONFLICT DO NOTHING`` keeps a concurrent double-apply
        harmless at the ledger level; the caller applies the balance
        delta under the collect lock so the balances can't race.
        """
        if not txns:
            return
        rows = [(t.id, account_id, int(link_id), -t.amount, _to_naive_utc(t.posted)) for t in txns]
        with self._lock:
            self.conn.executemany(
                """
                INSERT INTO transfer_link_applications
                    (transaction_id, account_id, link_id, amount, posted)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT (transaction_id, account_id) DO NOTHING
                """,
                rows,
            )

    @staticmethod
    def _row_to_transfer_link(row: tuple[Any, ...]) -> TransferLink:
        anchor = _from_naive_utc(row[7])
        created_at = _from_naive_utc(row[8])
        if anchor is None or created_at is None:  # pragma: no cover - NOT NULL columns
            raise StoreError("transfer link row is missing anchor or created_at")
        return TransferLink(
            id=int(row[0]),
            account_id=row[1],
            account_name=row[2],
            source_account_id=row[3],
            source_account_name=row[4],
            match_type=row[5],
            pattern=row[6],
            anchor=anchor,
            created_at=created_at,
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
                        cur = conn.execute(
                            statements[0], [_normalize_query_param(p) for p in params]
                        )
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
            is_hidden=bool(row[12]),
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
