"""Logical backup and restore for the local store.

Why a logical dump instead of copying ``data.duckdb``: neither file copy
nor DuckDB's own export is available while the daemon is running, which
is exactly when an automated backup has to work.

1. DuckDB holds an exclusive OS lock on the database file. On Windows
   that denies *any* other read handle — including one opened by the
   daemon process itself — so ``shutil.copy2`` raises PermissionError.
2. The store's connection sets ``enable_external_access=false`` (the
   third layer of ``sql_query``'s defense in depth, immutable once the
   database is running), which blocks ``EXPORT DATABASE``,
   ``COPY ... TO``, and ``ATTACH 'backup.duckdb'``.

So rows come out through the already-open connection as Python objects
and are written by Python. That is also the more durable artifact: the
archive is JSON Lines, readable without DuckDB and immune to the binary
corruption modes this database has actually hit (index corruption, WAL
replay failure).

Archive layout::

    goetta-backup-20260811T143000Z.zip
    ├── manifest.json      versions, migration stamp, row counts,
    │                      sequence positions, sha256 per file
    ├── RESTORE.md         how to restore, including by hand
    ├── data/<table>.jsonl header line of columns+types, then one JSON
    │                      array per row
    └── config/            prefixes.txt, config.json (access_url redacted)

Backups are written to a directory; uploading is deliberately not this
tool's job. Point the destination at a folder your cloud client already
syncs (OneDrive, Dropbox, Drive) and the sync happens without
goetta-finance making a single network call it didn't need — the
local-first rule in CLAUDE.md. The archive is built under a temporary
name and moved into place with ``os.replace`` only after it verifies,
so a sync client never uploads a partial or unrestorable file.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shutil
import tempfile
import zipfile
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from decimal import Decimal
from importlib.resources import files
from pathlib import Path
from typing import Any

import duckdb

from goetta_finance import __version__
from goetta_finance.errors import BackupError
from goetta_finance.store.duckdb_store import (
    RESTORE_ORDER,
    DuckDBStore,
    TableSnapshot,
)

logger = logging.getLogger(__name__)

# Bumped only for a change that older readers cannot parse. Restore
# refuses a higher version rather than guessing at unknown structure.
BACKUP_FORMAT_VERSION = 1

FILENAME_PREFIX = "goetta-backup-"
_TIMESTAMP_FORMAT = "%Y%m%dT%H%M%SZ"
# Anchored, and matched against the whole name: pruning deletes files
# from a directory the user also keeps other things in, so anything that
# is not exactly one of our archives is not ours to remove.
_FILENAME_RE = re.compile(r"^goetta-backup-(\d{8}T\d{6}Z)\.zip$")

_MANIFEST_NAME = "manifest.json"
_README_NAME = "RESTORE.md"
_DATA_PREFIX = "data/"
_CONFIG_PREFIX = "config/"

_REDACTED_KEYS = ("access_url",)

_RESTORE_README = """\
# Restoring this backup

    goetta-finance backup restore <this-file.zip>

Stop the daemon first (create a file named `daemon.stop` next to
`data.duckdb`; never force-kill it). Restore refuses to overwrite a
database in place — it moves the existing one aside as
`data.duckdb.pre-restore-<timestamp>` and builds a fresh one.

## Reading it without goetta-finance

`data/*.jsonl` is JSON Lines. The first line of each file is a header
naming the columns and their DuckDB types; every following line is one
row as a JSON array in that column order. `null` is SQL NULL. Money is
written as a *string* ("-31.02") so no float rounding can occur;
timestamps are ISO-8601.

`manifest.json` records which schema migrations the database had
applied. Restore replays migrations up to that stamp, loads the rows,
then applies any newer migrations on top, so an old archive still
restores into a current install.

## Not included

`config/config.json` has `access_url` removed — it is a live credential
for your bank feed, and this archive is meant to sit in cloud storage.
Re-claim a setup token from your SimpleFIN Bridge account and run
`goetta-finance init` after restoring.
"""


@dataclass(frozen=True)
class BackupResult:
    path: Path
    table_rows: dict[str, int]
    size_bytes: int
    verified: bool
    pruned: tuple[Path, ...] = ()

    @property
    def total_rows(self) -> int:
        return sum(self.table_rows.values())


@dataclass(frozen=True)
class BackupInfo:
    path: Path
    created_at: datetime
    size_bytes: int


@dataclass(frozen=True)
class RestoreResult:
    table_rows: dict[str, int]
    migrations_replayed: int
    migrations_applied_after: int
    sequences_advanced: dict[str, int] = field(default_factory=dict)
    # Seeded categories the archive lacked and DuckDB refused to delete.
    unremovable_category_ids: tuple[int, ...] = ()


# --- value encoding ---------------------------------------------------


def _encode_value(value: Any) -> Any:
    """Python -> JSON-safe, without losing precision or NULL-ness."""
    if value is None or isinstance(value, bool | int | str | float):
        return value
    if isinstance(value, Decimal):
        # str(), never float(): the whole point of Decimal money.
        return str(value)
    if isinstance(value, datetime | date):
        return value.isoformat()
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)


def _decode_value(value: Any, duckdb_type: str) -> Any:
    """JSON -> Python, driven by the column's declared type.

    Typed rather than value-shaped on purpose: a VARCHAR column holding
    something that merely looks like a date must come back as the string
    it was.
    """
    if value is None:
        return None
    upper = duckdb_type.upper()
    if upper.startswith("DECIMAL") or upper.startswith("NUMERIC"):
        return Decimal(str(value))
    if upper.startswith("TIMESTAMP"):
        return datetime.fromisoformat(str(value))
    if upper == "DATE":
        return date.fromisoformat(str(value))
    return value


def _snapshot_to_jsonl(snapshot: TableSnapshot) -> bytes:
    header = json.dumps(
        {
            "table": snapshot.name,
            "columns": list(snapshot.columns),
            "types": list(snapshot.types),
        },
        sort_keys=True,
    )
    lines = [header]
    lines.extend(
        json.dumps([_encode_value(v) for v in row], ensure_ascii=False) for row in snapshot.rows
    )
    return ("\n".join(lines) + "\n").encode("utf-8")


def _jsonl_to_snapshot(table: str, raw: bytes) -> TableSnapshot:
    text = raw.decode("utf-8")
    lines = [line for line in text.splitlines() if line.strip()]
    if not lines:
        raise BackupError(f"Empty data file for table {table}")
    try:
        header = json.loads(lines[0])
        columns = tuple(str(c) for c in header["columns"])
        types = tuple(str(t) for t in header["types"])
    except (json.JSONDecodeError, KeyError, TypeError) as exc:
        raise BackupError(f"Malformed header in data/{table}.jsonl: {exc}") from exc
    rows: list[tuple[Any, ...]] = []
    for lineno, line in enumerate(lines[1:], start=2):
        try:
            values = json.loads(line)
        except json.JSONDecodeError as exc:
            raise BackupError(f"Malformed row at data/{table}.jsonl:{lineno}: {exc}") from exc
        if len(values) != len(columns):
            raise BackupError(
                f"data/{table}.jsonl:{lineno} has {len(values)} values for {len(columns)} columns"
            )
        rows.append(tuple(_decode_value(v, t) for v, t in zip(values, types, strict=True)))
    return TableSnapshot(name=table, columns=columns, types=types, rows=tuple(rows))


# --- helpers ----------------------------------------------------------


def _available_migrations() -> list[str]:
    migrations_dir = files("goetta_finance.store.migrations")
    return sorted(entry.name for entry in migrations_dir.iterdir() if entry.name.endswith(".sql"))


def _redact_config(raw: bytes) -> bytes:
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        # Unparseable config is not worth failing a backup over, and we
        # must not ship bytes we could not inspect for credentials.
        logger.warning("config.json is not valid JSON; omitting it from the backup")
        return b""
    if not isinstance(data, dict):
        return b""
    for key in _REDACTED_KEYS:
        if data.get(key) is not None:
            data[key] = None
    return (json.dumps(data, indent=2, sort_keys=True) + "\n").encode("utf-8")


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _timestamp_name(moment: datetime) -> str:
    return f"{FILENAME_PREFIX}{moment.astimezone(UTC).strftime(_TIMESTAMP_FORMAT)}.zip"


def list_backups(directory: Path) -> list[BackupInfo]:
    """Archives in ``directory``, newest first. Ignores everything else."""
    if not directory.is_dir():
        return []
    found: list[BackupInfo] = []
    for entry in directory.iterdir():
        match = _FILENAME_RE.match(entry.name)
        if not match or not entry.is_file():
            continue
        try:
            created = datetime.strptime(match.group(1), _TIMESTAMP_FORMAT).replace(tzinfo=UTC)
        except ValueError:
            continue
        found.append(BackupInfo(path=entry, created_at=created, size_bytes=entry.stat().st_size))
    return sorted(found, key=lambda info: info.created_at, reverse=True)


def select_for_retention(
    backups: list[BackupInfo], *, keep_daily: int, keep_monthly: int
) -> list[BackupInfo]:
    """Split into keep/drop: the newest ``keep_daily`` archives, plus the
    oldest archive of each of the most recent ``keep_monthly`` months.

    The monthly tier exists because the daily tier cannot outlive its
    own window: if goetta-finance writes bad data and faithfully backs
    it up every day, fourteen dailies are fourteen copies of the damage.
    Keeping the first archive of each month leaves a pre-damage copy to
    fall back to. Returns the archives to DELETE.
    """
    keep: set[Path] = {info.path for info in backups[:keep_daily]}
    by_month: dict[tuple[int, int], BackupInfo] = {}
    for info in backups:
        key = (info.created_at.year, info.created_at.month)
        # backups is newest-first, so the last write per key is the oldest.
        by_month[key] = info
    for _key, info in sorted(by_month.items(), reverse=True)[:keep_monthly]:
        keep.add(info.path)
    return [info for info in backups if info.path not in keep]


# --- create -----------------------------------------------------------


def create_backup(
    store: DuckDBStore,
    directory: Path,
    *,
    config_file: Path | None = None,
    prefixes_file: Path | None = None,
    include_credentials: bool = False,
    verify: bool = True,
    keep_daily: int = 14,
    keep_monthly: int = 12,
    now: datetime | None = None,
) -> BackupResult:
    """Write one verified archive into ``directory`` and prune old ones.

    The archive is staged under a dotted temporary name in the same
    directory (so the final ``os.replace`` is atomic on one filesystem),
    verified by actually restoring it into a throwaway database, and
    only then moved to its real name. A cloud-sync client watching the
    folder therefore only ever sees archives that are known to restore.
    """
    moment = (now or datetime.now(UTC)).astimezone(UTC)
    directory.mkdir(parents=True, exist_ok=True)

    snapshots = store.snapshot_tables()
    sequences = store.sequence_positions()
    migrations = store.applied_migrations()

    payloads: dict[str, bytes] = {
        f"{_DATA_PREFIX}{snap.name}.jsonl": _snapshot_to_jsonl(snap) for snap in snapshots
    }
    if prefixes_file is not None and prefixes_file.is_file():
        payloads[f"{_CONFIG_PREFIX}prefixes.txt"] = prefixes_file.read_bytes()
    if config_file is not None and config_file.is_file():
        raw = config_file.read_bytes()
        body = raw if include_credentials else _redact_config(raw)
        if body:
            payloads[f"{_CONFIG_PREFIX}config.json"] = body

    table_rows = {snap.name: len(snap.rows) for snap in snapshots}
    manifest = {
        "format_version": BACKUP_FORMAT_VERSION,
        "created_at": moment.isoformat(),
        "goetta_finance_version": __version__,
        "duckdb_version": duckdb.__version__,
        "schema_migrations": migrations,
        "sequences": sequences,
        "tables": {
            snap.name: {"rows": len(snap.rows), "columns": list(snap.columns)} for snap in snapshots
        },
        "files": {name: _sha256(body) for name, body in sorted(payloads.items())},
        "credentials_included": include_credentials,
    }

    final_path = directory / _timestamp_name(moment)
    staged = directory / f".{final_path.name}.partial"
    try:
        with zipfile.ZipFile(staged, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
            zf.writestr(_MANIFEST_NAME, json.dumps(manifest, indent=2, sort_keys=True) + "\n")
            zf.writestr(_README_NAME, _RESTORE_README)
            for name, body in sorted(payloads.items()):
                zf.writestr(name, body)
        if verify:
            verify_backup(staged)
        size = staged.stat().st_size
        os.replace(staged, final_path)
    except BaseException:
        staged.unlink(missing_ok=True)
        raise

    logger.info(
        "backup written: %s (%d rows across %d tables, %d bytes, verified=%s)",
        final_path.name,
        sum(table_rows.values()),
        len(table_rows),
        size,
        verify,
    )

    pruned: list[Path] = []
    for stale in select_for_retention(
        list_backups(directory), keep_daily=keep_daily, keep_monthly=keep_monthly
    ):
        if stale.path == final_path:
            continue
        try:
            stale.path.unlink()
            pruned.append(stale.path)
        except OSError as exc:
            logger.warning("could not prune %s: %s", stale.path.name, exc)
    if pruned:
        logger.info("pruned %d expired backup(s)", len(pruned))

    return BackupResult(
        path=final_path,
        table_rows=table_rows,
        size_bytes=size,
        verified=verify,
        pruned=tuple(pruned),
    )


def maybe_create_daily_backup(
    store: DuckDBStore,
    directory: Path,
    *,
    now: datetime | None = None,
    **kwargs: Any,
) -> BackupResult | None:
    """``create_backup``, unless today already has one. Returns ``None``
    when it skipped.

    The daemon calls this after every successful sync rather than on its
    own clock, so archives track actual data change. The once-a-day
    guard is what keeps a day of catch-up syncs from writing a day of
    near-identical archives and pushing older ones out of the retention
    window. Dates are UTC, matching the archive filenames.
    """
    moment = (now or datetime.now(UTC)).astimezone(UTC)
    existing = list_backups(directory)
    if existing and existing[0].created_at.date() >= moment.date():
        logger.debug("backup skipped: %s already covers today", existing[0].path.name)
        return None
    return create_backup(store, directory, now=moment, **kwargs)


# --- read / verify / restore -----------------------------------------


def read_manifest(archive: Path) -> dict[str, Any]:
    """Parse and sanity-check an archive's manifest."""
    try:
        with zipfile.ZipFile(archive) as zf:
            raw = zf.read(_MANIFEST_NAME)
    except (OSError, zipfile.BadZipFile, KeyError) as exc:
        raise BackupError(f"Cannot read backup {archive.name}: {exc}") from exc
    try:
        manifest = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise BackupError(f"Corrupt manifest in {archive.name}: {exc}") from exc
    version = manifest.get("format_version")
    if not isinstance(version, int) or version > BACKUP_FORMAT_VERSION:
        raise BackupError(
            f"{archive.name} uses backup format {version}; this version of "
            f"goetta-finance reads up to {BACKUP_FORMAT_VERSION}. Upgrade first."
        )
    return dict(manifest)


def restore_backup(archive: Path, target_db: Path) -> RestoreResult:
    """Rebuild a database at ``target_db`` from ``archive``.

    ``target_db`` must not already exist — replacing a live database is
    the caller's decision to make explicitly (the CLI moves the old file
    aside rather than deleting it).

    Migrations are replayed to the stamp the archive carries, *then* the
    rows load, *then* any newer migrations run. That order is what lets
    a months-old archive restore into a current install with the
    data-transforming migrations (0006, 0007, 0014) still applied to it.
    """
    if target_db.exists():
        raise BackupError(f"Refusing to restore over an existing database at {target_db}")
    manifest = read_manifest(archive)

    stamped = [str(name) for name in manifest.get("schema_migrations", [])]
    available = set(_available_migrations())
    if unknown := sorted(set(stamped) - available):
        raise BackupError(
            f"{archive.name} was written by a newer goetta-finance: it carries "
            f"migration(s) this install does not have ({', '.join(unknown)}). "
            "Upgrade goetta-finance, then restore."
        )

    with zipfile.ZipFile(archive) as zf:
        names = set(zf.namelist())
        payloads = {
            table: zf.read(f"{_DATA_PREFIX}{table}.jsonl")
            for table in RESTORE_ORDER
            if f"{_DATA_PREFIX}{table}.jsonl" in names
        }

    target_db.parent.mkdir(parents=True, exist_ok=True)
    store = DuckDBStore(target_db)
    try:
        store.init(up_to=max(stamped) if stamped else None)
        replayed = len(store.applied_migrations())

        # Migrations seed rows (0004's default categories and rules), so
        # the freshly migrated database is not empty. The archive is the
        # authority on the user's rows — including the seeded ones they
        # deleted — so clear before loading.
        store.clear_restorable_tables()

        table_rows: dict[str, int] = {}
        archived_category_ids: list[int] = []
        for table in RESTORE_ORDER:
            if (raw := payloads.get(table)) is None:
                continue
            snapshot = _jsonl_to_snapshot(table, raw)
            if table == "categories":
                index = snapshot.columns.index("id")
                archived_category_ids = [int(row[index]) for row in snapshot.rows]
            table_rows[table] = store.restore_table(snapshot)

        # Seeded categories the archive does not contain cannot be
        # deleted (see DuckDBStore.clear_restorable_tables). Unreachable
        # today because no surface deletes a category, but report it
        # rather than let a restore quietly differ from its archive.
        leftover = store.unrestored_category_ids(archived_category_ids)
        if leftover:
            logger.warning(
                "%d seeded category row(s) could not be removed during restore "
                "(ids %s); they are not referenced by any restored row",
                len(leftover),
                ", ".join(str(i) for i in leftover),
            )

        # Sequences must land before the remaining migrations run, in
        # case one of those inserts a row of its own.
        advanced = store.align_sequences(dict(manifest.get("sequences", {})))

        store.init()
        applied_after = len(store.applied_migrations()) - replayed
        store.rebuild_category_match_cache()
    finally:
        store.close()

    logger.info(
        "restored %d rows across %d tables from %s",
        sum(table_rows.values()),
        len(table_rows),
        archive.name,
    )
    return RestoreResult(
        table_rows=table_rows,
        migrations_replayed=replayed,
        migrations_applied_after=applied_after,
        unremovable_category_ids=tuple(leftover),
        sequences_advanced=advanced,
    )


def verify_backup(archive: Path) -> dict[str, int]:
    """Check ``archive`` against its manifest, then test-restore it.

    This is the difference between "a zip was written" and "a backup
    exists". Three independent checks, because each catches something
    the others cannot:

    1. Every file's sha256 must match the manifest — bit rot, a
       truncated write, or an edited row.
    2. Restoring must reproduce the manifest's row counts.
    3. The amounts sitting in the restored database must sum to the same
       Decimal as the archive's rows — proof that what landed is what
       the archive says, not merely that the right *number* of rows
       landed. Note the limit of this one: a fault symmetric across both
       sides (a decoder both the archive read and the load share) shifts
       them equally and cancels out. It catches divergence introduced
       between decode and storage — a column-type coercion, a truncating
       cast, a sign flip — not a decoder that is uniformly wrong.
    """
    manifest = read_manifest(archive)
    expected = {str(table): int(meta["rows"]) for table, meta in manifest.get("tables", {}).items()}
    _verify_checksums(archive, manifest)
    workdir = Path(tempfile.mkdtemp(prefix="goetta-verify-"))
    restored_db = workdir / "verify.duckdb"
    try:
        restore_backup(archive, restored_db)
        # Read the rebuilt database back rather than trusting the restore
        # call's own return value — the point is to prove the bytes on
        # disk reproduce the data, not that the function counted its own
        # inserts correctly.
        store = DuckDBStore(restored_db, read_only=True)
        try:
            rebuilt = {snap.name: snap for snap in store.snapshot_tables()}
        finally:
            store.close()

        for table, want in expected.items():
            got = len(rebuilt[table].rows) if table in rebuilt else 0
            if got != want:
                raise BackupError(
                    f"Verification failed for {archive.name}: {table} restored "
                    f"{got} rows, manifest says {want}."
                )
        _verify_amounts(archive, rebuilt)
    finally:
        # Windows can still hold the freshly closed database file for a
        # moment; a leftover temp directory is not worth failing a good
        # backup over.
        shutil.rmtree(workdir, ignore_errors=True)
    return expected


def _verify_checksums(archive: Path, manifest: dict[str, Any]) -> None:
    """Every member the manifest lists must hash to what it recorded."""
    recorded = manifest.get("files", {})
    with zipfile.ZipFile(archive) as zf:
        present = set(zf.namelist())
        for name, digest in recorded.items():
            if name not in present:
                raise BackupError(f"{archive.name} is missing {name}, which its manifest lists.")
            if _sha256(zf.read(name)) != digest:
                raise BackupError(
                    f"{archive.name} failed its checksum on {name}: the file does "
                    "not match what was written."
                )


def _verify_amounts(archive: Path, rebuilt: dict[str, TableSnapshot]) -> None:
    """Cross-check restored money against the archive's own rows.

    Counting rows would not notice a Decimal that came back as a float
    or a sign that flipped; summing the amounts does.
    """
    if "transactions" not in rebuilt:
        return
    with zipfile.ZipFile(archive) as zf:
        from_archive = _jsonl_to_snapshot(
            "transactions", zf.read(f"{_DATA_PREFIX}transactions.jsonl")
        )

    def total(snapshot: TableSnapshot) -> Decimal:
        index = snapshot.columns.index("amount")
        return sum((row[index] for row in snapshot.rows), Decimal("0"))

    archived_total, restored_total = total(from_archive), total(rebuilt["transactions"])
    if archived_total != restored_total:
        raise BackupError(
            f"Verification failed for {archive.name}: restored transaction total "
            f"{restored_total} does not match the archive's {archived_total}."
        )
