from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

import pytest

from goetta_finance.store.duckdb_store import DuckDBStore

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def store(tmp_path: Path) -> Iterator[DuckDBStore]:
    """File-backed DuckDB store, fresh per test.

    File-backed (not :memory:) so the read-only connection sql_query opens
    can attach to the same database. tmp_path is on a fast local disk so the
    overhead vs :memory: is negligible for our test sizes.
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
