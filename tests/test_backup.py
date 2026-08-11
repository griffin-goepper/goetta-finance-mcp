from __future__ import annotations

import json
import zipfile
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from goetta_finance.backup import (
    BackupInfo,
    create_backup,
    list_backups,
    read_manifest,
    restore_backup,
    select_for_retention,
    verify_backup,
)
from goetta_finance.errors import BackupError, StoreError
from goetta_finance.models import (
    Account,
    AccountType,
    BalanceSnapshot,
    SyncRun,
    Transaction,
)
from goetta_finance.store.duckdb_store import RESTORE_ORDER, DuckDBStore, TableSnapshot


def _utc(year: int, month: int, day: int, hour: int = 12) -> datetime:
    return datetime(year, month, day, hour, tzinfo=UTC)


def _seed(store: DuckDBStore) -> None:
    """One row of every shape the encoder has to survive: Decimals, a
    NULL vs an empty string, JSON, unicode, a pending flag, and every
    table that carries a foreign key."""
    store.upsert_accounts(
        [
            Account(
                id="acc-1",
                org_id="org-1",
                org_name="Test Bank",
                name="Checking 1234",
                currency="USD",
                balance=Decimal("1234.56"),
                available_balance=Decimal("1200.00"),
                balance_date=_utc(2026, 5, 1),
                type=AccountType.CHECKING,
                extra={"nested": {"a": [1, 2, 3]}, "flag": True},
            ),
            Account(
                id="acc-2",
                org_id="org-1",
                org_name="Test Bank",
                name="Card 5678",
                currency="USD",
                balance=Decimal("-402.10"),
                balance_date=_utc(2026, 5, 1),
                type=AccountType.CREDIT,
            ),
            # Manual account: transfer links only roll forward these.
            Account(
                id="acc-manual",
                name="Brokerage",
                currency="USD",
                balance=Decimal("5000.00"),
                balance_date=_utc(2026, 5, 1),
                type=AccountType.INVESTMENT,
                is_manual=True,
            ),
        ]
    )
    store.set_account_liability("acc-2", True)
    store.set_account_hidden("acc-2", True)
    store.upsert_transactions(
        [
            Transaction(
                id="t-plain",
                account_id="acc-1",
                posted=_utc(2026, 5, 10),
                transacted_at=_utc(2026, 5, 9),
                amount=Decimal("-31.02"),
                description="KROGER #123",
                payee="Kroger",
                memo="",  # empty string, NOT null — must stay distinguishable
            ),
            Transaction(
                id="t-null-payee",
                account_id="acc-1",
                posted=_utc(2026, 5, 11),
                amount=Decimal("0.05"),
                description="Café ☕ interest — ünicode",
                payee=None,
                memo=None,
            ),
            Transaction(
                id="t-pending",
                account_id="acc-2",
                posted=_utc(2026, 5, 12),
                amount=Decimal("-9999999.99"),
                description="STARBUCKS STORE #1",
                pending=True,
                extra={"source": "test"},
            ),
        ]
    )
    store.record_balance_snapshot(
        BalanceSnapshot(account_id="acc-1", timestamp=_utc(2026, 5, 1), balance=Decimal("1234.56"))
    )
    store.record_sync_run(
        SyncRun(
            started_at=_utc(2026, 5, 1, 1),
            finished_at=_utc(2026, 5, 1, 2),
            accounts_touched=2,
            transactions_new=3,
            transactions_updated=0,
            warnings=["a warning"],
            errors=[],
        )
    )
    # A user-added category, not one of 0004's seeded defaults (adding
    # "Groceries" would collide with the seed's UNIQUE name). Priority 1
    # so it outranks the seeded KROGER rule.
    store.add_category("Grocery Runs")
    store.add_rule("Grocery Runs", match_type="contains", pattern="KROGER", priority=1)
    store.set_transaction_override("t-null-payee", "Grocery Runs")
    store.add_goal(
        "Grocery cap",
        kind="spending_cap",
        amount=Decimal("500.00"),
        category_name="Grocery Runs",
        period="month",
    )
    store.add_transfer_link("acc-manual", "acc-1", match_type="contains", pattern="PAYMENT")


def _dump_all(store: DuckDBStore) -> dict[str, list[tuple[object, ...]]]:
    return {snap.name: sorted(snap.rows, key=repr) for snap in store.snapshot_tables()}


@pytest.fixture
def seeded(store: DuckDBStore) -> DuckDBStore:
    _seed(store)
    return store


def _backup_and_restore(store: DuckDBStore, tmp_path: Path, **kwargs: object) -> DuckDBStore:
    """Back up ``store``, restore into a fresh file, return the new store.
    Caller closes it."""
    result = create_backup(store, tmp_path / "backups", **kwargs)  # type: ignore[arg-type]
    target = tmp_path / "restored" / "data.duckdb"
    restore_backup(result.path, target)
    return DuckDBStore(target)


# --- round-trip fidelity ---------------------------------------------


def test_round_trip_preserves_every_table_exactly(seeded: DuckDBStore, tmp_path: Path) -> None:
    before = _dump_all(seeded)
    restored = _backup_and_restore(seeded, tmp_path)
    try:
        assert _dump_all(restored) == before
    finally:
        restored.close()


def test_round_trip_preserves_null_versus_empty_string(seeded: DuckDBStore, tmp_path: Path) -> None:
    """The classic CSV-shaped data loss: '' and NULL collapsing into each
    other. JSON Lines keeps them distinct and this pins it."""
    restored = _backup_and_restore(seeded, tmp_path)
    try:
        rows = restored.conn.execute(
            "SELECT id, payee, memo FROM transactions WHERE id IN ('t-plain', 't-null-payee') "
            "ORDER BY id"
        ).fetchall()
    finally:
        restored.close()
    assert rows == [
        ("t-null-payee", None, None),
        ("t-plain", "Kroger", ""),
    ]


def test_round_trip_keeps_money_exact_as_decimal(seeded: DuckDBStore, tmp_path: Path) -> None:
    restored = _backup_and_restore(seeded, tmp_path)
    try:
        rows = dict(restored.conn.execute("SELECT id, amount FROM transactions").fetchall())
    finally:
        restored.close()
    assert rows["t-plain"] == Decimal("-31.02")
    assert isinstance(rows["t-plain"], Decimal)
    assert rows["t-null-payee"] == Decimal("0.05")
    assert rows["t-pending"] == Decimal("-9999999.99")


def test_round_trip_preserves_user_owned_account_flags(seeded: DuckDBStore, tmp_path: Path) -> None:
    restored = _backup_and_restore(seeded, tmp_path)
    try:
        row = restored.conn.execute(
            "SELECT is_liability, is_hidden FROM accounts WHERE id = 'acc-2'"
        ).fetchone()
    finally:
        restored.close()
    assert row == (True, True)


def test_restore_rebuilds_the_category_match_cache(seeded: DuckDBStore, tmp_path: Path) -> None:
    """The cache is derived, so it is never dumped — which means restore
    has to rebuild it or every restored transaction reads back as
    Uncategorized."""
    restored = _backup_and_restore(seeded, tmp_path)
    try:
        assert restored.conn.execute("SELECT count(*) FROM category_match_cache").fetchone()[0] > 0
        category = restored.conn.execute(
            "SELECT category FROM transactions_with_category WHERE id = 't-plain'"
        ).fetchone()[0]
    finally:
        restored.close()
    assert category == "Grocery Runs"


# --- sequences --------------------------------------------------------


def test_restore_advances_sequences_past_restored_ids(seeded: DuckDBStore, tmp_path: Path) -> None:
    """DuckDB cannot reposition a sequence a column DEFAULT depends on
    (CREATE OR REPLACE and DROP raise DependencyException, ALTER ...
    RESTART is unimplemented), so restore burns it forward with
    nextval(). Without that, the first row inserted after a restore
    collides with a restored primary key."""
    highest_rule = seeded.conn.execute("SELECT max(id) FROM category_rules").fetchone()[0]
    restored = _backup_and_restore(seeded, tmp_path)
    try:
        new_rule_id = restored.add_rule("Grocery Runs", match_type="contains", pattern="ALDI")
        assert new_rule_id > highest_rule

        new_category = restored.add_category("Fuel")
        assert new_category.id > 1

        goal = restored.add_goal(
            "Fuel cap",
            kind="spending_cap",
            amount=Decimal("100.00"),
            category_name="Fuel",
            period="month",
        )
        assert goal.id > 1
    finally:
        restored.close()


# --- migration stamps -------------------------------------------------


def test_restore_replays_newer_migrations_on_top_of_an_older_archive(
    tmp_path: Path,
) -> None:
    """An archive taken at an older schema must still restore into a
    current install, with the migrations it never saw applied on top."""
    old = DuckDBStore(tmp_path / "old.duckdb")
    old.init(up_to="0014_contribution_goals.sql")
    try:
        assert "0015_recurring_contributions.sql" not in old.applied_migrations()
        old.upsert_accounts(
            [
                Account(
                    id="acc-1",
                    name="Checking 1234",
                    currency="USD",
                    balance=Decimal("10.00"),
                    balance_date=_utc(2026, 5, 1),
                )
            ]
        )
        result = create_backup(old, tmp_path / "backups")
    finally:
        old.close()

    target = tmp_path / "restored.duckdb"
    restored_result = restore_backup(result.path, target)
    assert restored_result.migrations_applied_after == 1

    store = DuckDBStore(target)
    try:
        assert "0015_recurring_contributions.sql" in store.applied_migrations()
        # The column 0015 adds exists, and the older row survived.
        assert store.conn.execute("SELECT recurring_amount FROM goals").fetchall() == []
        assert store.conn.execute("SELECT count(*) FROM accounts").fetchone()[0] == 1
    finally:
        store.close()


def test_restore_refuses_an_archive_from_a_newer_version(
    seeded: DuckDBStore, tmp_path: Path
) -> None:
    result = create_backup(seeded, tmp_path / "backups")
    tampered = tmp_path / "from-the-future.zip"
    _rewrite_manifest(
        result.path,
        tampered,
        lambda m: {**m, "schema_migrations": [*m["schema_migrations"], "0099_time_travel.sql"]},
    )
    with pytest.raises(BackupError, match="newer goetta-finance"):
        restore_backup(tampered, tmp_path / "restored.duckdb")


def test_read_manifest_refuses_an_unknown_format_version(
    seeded: DuckDBStore, tmp_path: Path
) -> None:
    result = create_backup(seeded, tmp_path / "backups")
    tampered = tmp_path / "future-format.zip"
    _rewrite_manifest(result.path, tampered, lambda m: {**m, "format_version": 99})
    with pytest.raises(BackupError, match="format 99"):
        read_manifest(tampered)


def test_restore_refuses_to_overwrite_an_existing_database(
    seeded: DuckDBStore, tmp_path: Path
) -> None:
    result = create_backup(seeded, tmp_path / "backups")
    existing = tmp_path / "already-here.duckdb"
    existing.write_bytes(b"not really a database")
    with pytest.raises(BackupError, match="Refusing to restore over"):
        restore_backup(result.path, existing)
    assert existing.read_bytes() == b"not really a database"


# --- verification is load-bearing ------------------------------------


def test_verify_rejects_a_tampered_data_file(seeded: DuckDBStore, tmp_path: Path) -> None:
    """Layer 1: a single altered digit fails the manifest checksum."""
    result = create_backup(seeded, tmp_path / "backups")
    altered = tmp_path / "altered.zip"
    _rewrite_member(
        result.path,
        altered,
        "data/transactions.jsonl",
        lambda raw: raw.replace(b'"-31.02"', b'"-31.03"'),
    )
    with pytest.raises(BackupError, match="failed its checksum"):
        verify_backup(altered)


def test_verify_rejects_an_archive_with_a_missing_row(seeded: DuckDBStore, tmp_path: Path) -> None:
    """Layer 2: row counts. Checksums are repaired first so the count
    check is what actually fires — otherwise this would only re-test the
    layer above it."""
    result = create_backup(seeded, tmp_path / "backups")
    truncated = tmp_path / "truncated.zip"
    _rewrite_member(
        result.path,
        truncated,
        "data/transactions.jsonl",
        lambda raw: b"".join(line + b"\n" for line in raw.splitlines()[:-1]),
        fix_checksums=True,
    )
    with pytest.raises(BackupError, match="transactions restored"):
        verify_backup(truncated)


def test_verify_rejects_a_load_that_changes_the_money(
    seeded: DuckDBStore, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Layer 3: the archive is intact and the row counts are right, but
    what landed in the database is not what the archive says.

    Simulated by corrupting the insert path rather than the decoder — a
    decoder both sides share would shift them equally and cancel out,
    which is precisely why this layer compares the restored *database*
    against the archive instead of re-reading the archive twice.
    """
    result = create_backup(seeded, tmp_path / "backups")
    real_restore = DuckDBStore.restore_table

    def sign_flipping(self: DuckDBStore, snapshot: TableSnapshot) -> int:
        if snapshot.name == "transactions":
            index = snapshot.columns.index("amount")
            snapshot = TableSnapshot(
                name=snapshot.name,
                columns=snapshot.columns,
                types=snapshot.types,
                rows=tuple(
                    tuple(-value if i == index else value for i, value in enumerate(row))
                    for row in snapshot.rows
                ),
            )
        return real_restore(self, snapshot)

    monkeypatch.setattr(DuckDBStore, "restore_table", sign_flipping)
    with pytest.raises(BackupError, match="does not match the archive"):
        verify_backup(result.path)


def test_an_unverifiable_archive_never_lands_in_the_directory(
    seeded: DuckDBStore, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The staged file is moved into place only after it verifies, so a
    cloud-sync client watching the folder cannot pick up a bad archive
    — nor a half-written one."""
    dest = tmp_path / "backups"

    def boom(_archive: Path) -> dict[str, int]:
        raise BackupError("simulated verification failure")

    monkeypatch.setattr("goetta_finance.backup.verify_backup", boom)
    with pytest.raises(BackupError, match="simulated"):
        create_backup(seeded, dest)

    assert list_backups(dest) == []
    assert list(dest.iterdir()) == []  # no leftover .partial either


# --- credentials ------------------------------------------------------


def test_access_url_is_redacted_by_default(seeded: DuckDBStore, tmp_path: Path) -> None:
    config = tmp_path / "config.json"
    config.write_text(
        json.dumps(
            {"access_url": "https://user:secret@bridge.example/simplefin", "backend": "duckdb"}
        ),
        encoding="utf-8",
    )
    result = create_backup(seeded, tmp_path / "backups", config_file=config)

    with zipfile.ZipFile(result.path) as zf:
        stored = json.loads(zf.read("config/config.json"))
        raw_archive = zf.read("config/config.json")
    assert stored["access_url"] is None
    assert stored["backend"] == "duckdb"
    assert b"secret" not in raw_archive


def test_access_url_is_included_only_when_asked(seeded: DuckDBStore, tmp_path: Path) -> None:
    config = tmp_path / "config.json"
    config.write_text(json.dumps({"access_url": "https://user:secret@bridge.example"}), "utf-8")
    result = create_backup(
        seeded, tmp_path / "backups", config_file=config, include_credentials=True
    )
    with zipfile.ZipFile(result.path) as zf:
        stored = json.loads(zf.read("config/config.json"))
    assert stored["access_url"] == "https://user:secret@bridge.example"
    assert read_manifest(result.path)["credentials_included"] is True


# --- structural guard -------------------------------------------------


def test_snapshot_refuses_an_unclassified_table(store: DuckDBStore) -> None:
    """A future migration that adds a table must classify it. Failing
    loudly here beats every backup from then on silently omitting it."""
    store.conn.execute("CREATE TABLE surprise (id INTEGER)")
    with pytest.raises(StoreError, match="not classified for backup: surprise"):
        store.snapshot_tables()


def test_restore_order_places_every_table_after_its_dependencies() -> None:
    """accounts before anything referencing it; categories before the
    tables whose foreign keys point at it."""
    position = {table: index for index, table in enumerate(RESTORE_ORDER)}
    for parent, child in [
        ("accounts", "transactions"),
        ("accounts", "balance_snapshots"),
        ("accounts", "goals"),
        ("accounts", "transfer_links"),
        ("categories", "category_rules"),
        ("categories", "transaction_overrides"),
        ("categories", "goals"),
    ]:
        assert position[parent] < position[child], f"{parent} must load before {child}"


# --- retention --------------------------------------------------------


def _info(day: str) -> BackupInfo:
    return BackupInfo(
        path=Path(f"goetta-backup-{day}.zip"),
        created_at=datetime.strptime(day, "%Y%m%dT%H%M%SZ").replace(tzinfo=UTC),
        size_bytes=100,
    )


def test_retention_keeps_recent_dailies_and_one_archive_per_month() -> None:
    backups = [
        _info((datetime(2026, 8, 11, tzinfo=UTC) - timedelta(days=n)).strftime("%Y%m%dT%H%M%SZ"))
        for n in range(120)
    ]
    dropped = {info.path for info in select_for_retention(backups, keep_daily=14, keep_monthly=12)}
    kept = [info for info in backups if info.path not in dropped]

    # The 14 newest survive...
    assert all(info.path not in dropped for info in backups[:14])
    # ...and each month covered still has exactly one representative left,
    # so a bad month's worth of dailies can't push out every older copy.
    months = {(info.created_at.year, info.created_at.month) for info in backups}
    kept_months = {(info.created_at.year, info.created_at.month) for info in kept}
    assert kept_months == months


def test_retention_keeps_the_oldest_archive_of_each_month() -> None:
    backups = [_info(d) for d in ("20260715T000000Z", "20260710T000000Z", "20260701T000000Z")]
    dropped = select_for_retention(backups, keep_daily=1, keep_monthly=1)
    assert [info.path.name for info in dropped] == ["goetta-backup-20260710T000000Z.zip"]


def test_pruning_only_ever_deletes_our_own_archives(seeded: DuckDBStore, tmp_path: Path) -> None:
    """The destination is meant to be a cloud-synced folder the user also
    keeps other things in. Nothing that isn't exactly one of our archives
    is ours to delete."""
    dest = tmp_path / "backups"
    dest.mkdir()
    bystanders = [dest / "tax-return-2025.pdf", dest / "goetta-backup-notes.txt", dest / "old.zip"]
    for path in bystanders:
        path.write_text("keep me", encoding="utf-8")

    create_backup(seeded, dest, keep_daily=0, keep_monthly=0)
    for path in bystanders:
        assert path.exists(), f"{path.name} was deleted"


def test_backup_prunes_expired_archives(seeded: DuckDBStore, tmp_path: Path) -> None:
    dest = tmp_path / "backups"
    first = create_backup(seeded, dest, now=datetime(2026, 1, 1, tzinfo=UTC))
    create_backup(seeded, dest, now=datetime(2026, 6, 1, tzinfo=UTC))
    latest = create_backup(
        seeded, dest, now=datetime(2026, 6, 2, tzinfo=UTC), keep_daily=1, keep_monthly=1
    )

    remaining = {info.path.name for info in list_backups(dest)}
    assert latest.path.name in remaining
    assert first.path.name not in remaining
    assert first.path in latest.pruned


def test_list_backups_ignores_foreign_files_and_sorts_newest_first(tmp_path: Path) -> None:
    dest = tmp_path / "backups"
    dest.mkdir()
    (dest / "goetta-backup-20260101T000000Z.zip").write_bytes(b"x")
    (dest / "goetta-backup-20260601T000000Z.zip").write_bytes(b"x")
    (dest / "not-a-backup.zip").write_bytes(b"x")
    (dest / ".goetta-backup-20260701T000000Z.zip.partial").write_bytes(b"x")

    assert [info.path.name for info in list_backups(dest)] == [
        "goetta-backup-20260601T000000Z.zip",
        "goetta-backup-20260101T000000Z.zip",
    ]


# --- manifest ---------------------------------------------------------


def test_manifest_records_counts_checksums_and_the_migration_stamp(
    seeded: DuckDBStore, tmp_path: Path
) -> None:
    import hashlib

    result = create_backup(seeded, tmp_path / "backups")
    manifest = read_manifest(result.path)

    assert manifest["tables"]["transactions"]["rows"] == 3
    assert manifest["schema_migrations"] == seeded.applied_migrations()
    assert manifest["sequences"]["category_rules_id_seq"] is not None

    with zipfile.ZipFile(result.path) as zf:
        for name, digest in manifest["files"].items():
            assert hashlib.sha256(zf.read(name)).hexdigest() == digest, name
        assert "RESTORE.md" in zf.namelist()


def test_derived_cache_is_not_shipped_in_the_archive(seeded: DuckDBStore, tmp_path: Path) -> None:
    result = create_backup(seeded, tmp_path / "backups")
    with zipfile.ZipFile(result.path) as zf:
        assert "data/category_match_cache.jsonl" not in zf.namelist()
        assert "data/schema_migrations.jsonl" not in zf.namelist()


def test_backup_of_an_empty_database_round_trips(store: DuckDBStore, tmp_path: Path) -> None:
    result = create_backup(store, tmp_path / "backups")
    assert result.verified
    restored_result = restore_backup(result.path, tmp_path / "restored.duckdb")
    assert restored_result.table_rows["transactions"] == 0


# --- helpers ----------------------------------------------------------


def _rewrite_manifest(source: Path, target: Path, mutate: object) -> None:
    _rewrite_member(
        source,
        target,
        "manifest.json",
        lambda raw: json.dumps(mutate(json.loads(raw))).encode("utf-8"),  # type: ignore[operator]
    )


def _rewrite_member(
    source: Path,
    target: Path,
    member: str,
    mutate: object,
    *,
    fix_checksums: bool = False,
) -> None:
    """Copy an archive, transforming one member. Used to forge the
    corrupt archives verification is supposed to reject.

    ``fix_checksums`` re-stamps the manifest so the forged archive gets
    past the checksum layer — needed to test the layers beneath it."""
    import hashlib

    with zipfile.ZipFile(source) as src:
        members = {info.filename: src.read(info.filename) for info in src.infolist()}
    members[member] = mutate(members[member])  # type: ignore[operator]
    if fix_checksums:
        manifest = json.loads(members["manifest.json"])
        manifest["files"][member] = hashlib.sha256(members[member]).hexdigest()
        members["manifest.json"] = json.dumps(manifest).encode("utf-8")
    with zipfile.ZipFile(target, "w") as dst:
        for name, raw in members.items():
            dst.writestr(name, raw)
