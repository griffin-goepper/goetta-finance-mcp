from __future__ import annotations

from typing import Any

from goetta_finance.store import FinanceStore
from goetta_finance.tools._serialize import serialize_value


def sql_query(store: FinanceStore, sql: str) -> list[dict[str, Any]]:
    rows = store.query_sql(sql)
    return [{k: serialize_value(v) for k, v in row.items()} for row in rows]
