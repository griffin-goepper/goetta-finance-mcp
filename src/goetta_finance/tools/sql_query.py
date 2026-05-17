from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any

from goetta_finance.store import FinanceStore


def _serialize_value(value: Any) -> Any:
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, datetime | date):
        return value.isoformat()
    return value


def sql_query(store: FinanceStore, sql: str) -> list[dict[str, Any]]:
    rows = store.query_sql(sql)
    return [{k: _serialize_value(v) for k, v in row.items()} for row in rows]
