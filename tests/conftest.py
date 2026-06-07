from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

import pytest

from goetta_finance.store.duckdb_store import DuckDBStore

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def store(tmp_path: Path) -> Iterator[DuckDBStore]:
    """File-backed DuckDB store, fresh per test, with the legacy
    merchant rules pre-seeded.

    Migration 0007 demoted ~30 merchant-specific default rules so the
    codebase ships universally minimal. Most existing tests were written
    before 0007 and used STARBUCKS / KROGER / SHELL / AMAZON.COM as
    convenient examples that happened to match a default rule — they're
    testing rule-resolution behavior, not those specific merchants. The
    auto-seed restores those rules as user-added so existing tests stay
    valid. NEW tests written for post-0007 behavior should use the
    ``store_no_legacy_rules`` fixture instead (defined below) if they
    need the post-0007 minimal default state explicitly.

    File-backed (not :memory:) so the read-only connection sql_query
    opens can attach to the same database.
    """
    store = DuckDBStore(tmp_path / "test.duckdb")
    store.init()
    seed_legacy_merchant_rules(store)
    try:
        yield store
    finally:
        store.close()


@pytest.fixture
def store_no_legacy_rules(tmp_path: Path) -> Iterator[DuckDBStore]:
    """Same as ``store`` but without the legacy merchant rules auto-seed.

    Use for tests that need to assert post-0007 behavior explicitly
    (e.g. ``test_migration_0007_keeps_only_universal_default_rules``).
    """
    store = DuckDBStore(tmp_path / "test.duckdb")
    store.init()
    try:
        yield store
    finally:
        store.close()


@pytest.fixture
def demo_response() -> dict:
    with (FIXTURES / "simplefin_demo_response.json").open("r", encoding="utf-8") as f:
        return json.load(f)


def seed_legacy_merchant_rules(store: DuckDBStore) -> None:
    """Add the merchant-specific rules that migration 0007 demoted.

    Tests written before 0007 used STARBUCKS / KROGER / SHELL / AMAZON.COM
    as convenient examples that "happened to match a default rule" — they
    were really testing the rule-resolution behavior, not those merchants
    specifically. After 0007 those defaults are gone, so this helper
    restores them as user-added rules when a test needs them.

    NEW tests written for a feature unrelated to categorization should
    NOT use this helper — they should seed only the specific rule they
    need, or use SPOTIFY/NETFLIX/etc. which survived as default rules.
    """
    store.add_rule("Dining", match_type="contains", pattern="STARBUCKS")
    store.add_rule("Groceries", match_type="contains", pattern="KROGER")
    store.add_rule("Gas", match_type="contains", pattern="SHELL")
    store.add_rule("Shopping", match_type="contains", pattern="AMAZON.COM", priority=50)
