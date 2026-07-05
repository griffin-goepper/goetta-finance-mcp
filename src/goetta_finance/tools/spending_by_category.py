from __future__ import annotations

from datetime import datetime
from typing import Any

from goetta_finance.store import FinanceStore
from goetta_finance.tools._serialize import serialize_value


def query_spending_by_category(
    store: FinanceStore,
    start: datetime,
    end: datetime,
    *,
    include_non_spending: bool = False,
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

    Semantics: see ``spending_by_category`` docstring below. NET spending
    — refunds (positive amounts in a categorized spending category)
    reduce that category's total. A positive amount in ``Uncategorized``
    contributes 0 (it's ambiguous until categorized — not a phantom
    refund). The SAME net-spending CASE drives the monthly-bars
    aggregation (``web/aggregations.monthly_income_spending``), so the
    pie and the bars agree to the cent — pinned by
    ``test_pie_and_monthly_bar_agree_on_net_spending``.
    """
    # Hidden accounts always filtered. Category membership filtered by the
    # is_spending flag (migration 0006): default mode shows only spending
    # categories as rows; include_non_spending shows all. There is no
    # amount filter — net spending counts both signs (the CASE handles the
    # uncategorized-positive guard).
    base_where = "t.posted >= ? AND t.posted <= ? AND COALESCE(t.account_is_hidden, FALSE) = FALSE"
    if include_non_spending:
        where = base_where
    else:
        where = f"{base_where} AND COALESCE(c.is_spending, TRUE) = TRUE"
    # Net-spending total per category: -amount for every transaction
    # EXCEPT a positive amount in Uncategorized, which contributes 0.
    # (Non-spending categories are excluded by the WHERE in default mode;
    # in include_non_spending mode they appear with their full -amount sum,
    # so Income shows negative / Transfers positive — sign conveys
    # direction.)
    total_expr = (
        "SUM(CASE WHEN t.amount > 0 AND t.category = 'Uncategorized' THEN 0 ELSE -t.amount END)"
    )
    # ruff S608 / bandit B608: ``where`` and ``total_expr`` are composed
    # entirely of string literals plus ``?`` placeholders that bind via the
    # params list. No user input is interpolated. Audited 2026-05.
    sql = f"""
        SELECT t.category, {total_expr} AS total, COUNT(*) AS transaction_count
        FROM transactions_with_category t
        LEFT JOIN categories c ON c.name = t.category
        WHERE {where}
        GROUP BY t.category
        ORDER BY total DESC
    """  # noqa: S608  # nosec B608
    return store.query_sql(sql, [start, end])


def spending_by_category(
    store: FinanceStore,
    start: datetime,
    end: datetime,
    *,
    include_non_spending: bool = False,
) -> list[dict[str, Any]]:
    """Aggregate NET spending totals per category between ``start`` and ``end``.

    Net = spending minus refunds. A refund (positive amount in a
    categorized spending category, e.g. a Dining return) reduces that
    category's total: a $50 Dining charge + $10 Dining refund = $40.

    Default mode (``include_non_spending=False``):
        Spending categories only (``c.is_spending = TRUE``). Each total
        is ``SUM(-amount)`` over the category's transactions, both signs,
        EXCEPT a positive amount in ``Uncategorized`` which contributes 0
        (ambiguous credit, not a phantom refund). Totals come back
        positive (a category dominated by refunds could go negative,
        which is correct).

    ``include_non_spending=True``:
        All categories appear. Income transactions are positive amounts,
        so ``SUM(-amount)`` returns a NEGATIVE total — sign conveys "cash
        in." Transfers are negative on the source account → POSITIVE
        total (money moving to your own accounts, not spending).

    Both modes group by category, sort by total descending. Hidden-
    account transactions are always excluded. The same net-spending
    expression drives the dashboard's monthly bars, so the two surfaces
    agree to the cent.
    """
    rows = query_spending_by_category(store, start, end, include_non_spending=include_non_spending)
    return [{k: serialize_value(v) for k, v in row.items()} for row in rows]
