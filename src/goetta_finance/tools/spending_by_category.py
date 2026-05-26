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


def query_spending_by_category(
    store: FinanceStore,
    start: datetime,
    end: datetime,
    *,
    include_income: bool = False,
) -> list[dict[str, Any]]:
    """Shared SQL helper — raw store rows, Decimal totals.

    Public callers:
      - ``spending_by_category`` (this module) — wraps with JSON-friendly
        serialization for the MCP tool surface.
      - ``web/aggregations.py:spending_by_category_last_n_days`` — keeps
        Decimals so the dashboard's pie chart downcasts to float in one
        place (the chart builder), matching the existing pattern in
        ``net_worth_series`` / ``monthly_income_spending``.

    Factored when the second caller (dashboard) arrived in sub-seam 4 —
    rule of three's predecessor. If a third caller emerges and the
    parameter shape gets awkward, this is the right place to widen.

    Semantics: see ``spending_by_category`` docstring below.
    """
    # Always filter transactions from hidden accounts. The MCP tool / web
    # surface don't expose an include_hidden flag — hiding is a user
    # statement that "this account doesn't count," and bleeding its
    # transactions into category totals would defeat the point. Users who
    # really want raw numbers reach for sql_query against the view
    # directly.
    base_where = "posted >= ? AND posted <= ? AND COALESCE(account_is_hidden, FALSE) = FALSE"
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
    return store.query_sql(sql, [start, end])


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
    rows = query_spending_by_category(store, start, end, include_income=include_income)
    return [{k: _serialize_value(v) for k, v in row.items()} for row in rows]
