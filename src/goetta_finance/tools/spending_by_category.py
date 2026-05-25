from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any

from goetta_finance.store import FinanceStore


def _serialize_value(value: Any) -> Any:
    """JSON-friendly conversion for Decimal/datetime — third copy of this
    helper in ``tools/`` (after ``sql_query.py`` and ``transactions.py``).

    NOTE: Per the categorization slice plan's "rule of three with explicit
    defer" — this is the third place we serialize Decimals / datetimes in
    tools/. Worth a small follow-on slice to factor into
    ``tools/_serialize.py`` when a fourth copy emerges or before the
    sub-seam 4 dashboard work if its row serialization wants the same
    shape. Logged here so the duplication doesn't quietly become
    permanent.
    """
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, datetime | date):
        return value.isoformat()
    return value


def spending_by_category(
    store: FinanceStore,
    start: datetime,
    end: datetime,
    *,
    include_income: bool = False,
) -> list[dict[str, Any]]:
    """Aggregate spending totals per category between ``start`` and ``end``.

    Default mode (``include_income=False``):
        Sums ``SUM(-amount)`` over rows with ``amount < 0`` AND
        ``category <> 'Income'``. Spending categories come back with
        positive totals.

    ``include_income=True``:
        Widens the filter to ``amount < 0 OR category = 'Income'``.
        The Income category's source transactions are positive amounts,
        so ``SUM(-amount)`` returns a negative total — sign conveys
        direction (cash in). Spending categories are unchanged.

    Both modes group by category, sort by total descending.

    Refunds in non-Income categories (positive ``amount``, e.g. Dining
    refund) are NOT counted in either mode — the "amount < 0" filter
    enforces the literal "spending = money out" contract documented in
    the tool description. If dogfooding shows this matters for net-of-
    refunds analyses, a future ``include_refunds`` flag can be added.
    """
    base_where = "posted >= ? AND posted <= ?"
    if include_income:
        where = f"{base_where} AND (amount < 0 OR category = 'Income')"
    else:
        where = f"{base_where} AND amount < 0 AND category <> 'Income'"
    # ruff S608 / bandit B608: ``where`` is composed entirely of string
    # literals plus ``?`` placeholders that bind via the params list. No
    # user input is interpolated. Audited 2026-05.
    sql = f"""
        SELECT category, SUM(-amount) AS total, COUNT(*) AS transaction_count
        FROM transactions_with_category
        WHERE {where}
        GROUP BY category
        ORDER BY total DESC
    """  # noqa: S608  # nosec B608
    rows = store.query_sql(sql, [start, end])
    return [{k: _serialize_value(v) for k, v in row.items()} for row in rows]
