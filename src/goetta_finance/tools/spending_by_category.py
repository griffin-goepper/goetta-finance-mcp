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

    Semantics: see ``spending_by_category`` docstring below. Default
    behavior filters non-spending categories (Transfers, Income, any
    category with ``is_spending=FALSE``) via a JOIN to the categories
    table on the resolved category name.
    """
    # Filter transactions from hidden accounts always — hiding is a user
    # statement that "this account doesn't count." Filter non-spending
    # categories by default — driven by the categories.is_spending flag
    # introduced in migration 0006, which replaced the hardcoded
    # ``category <> 'Income'`` filter (Transfers, etc. needed similar
    # treatment and a schema flag is the principled answer).
    base_where = "t.posted >= ? AND t.posted <= ? AND COALESCE(t.account_is_hidden, FALSE) = FALSE"
    if include_non_spending:
        where = f"{base_where} AND (t.amount < 0 OR COALESCE(c.is_spending, TRUE) = FALSE)"
    else:
        where = f"{base_where} AND t.amount < 0 AND COALESCE(c.is_spending, TRUE) = TRUE"
    # ruff S608 / bandit B608: ``where`` is composed entirely of string
    # literals plus ``?`` placeholders that bind via the params list. No
    # user input is interpolated. Audited 2026-05.
    sql = f"""
        SELECT t.category, SUM(-t.amount) AS total, COUNT(*) AS transaction_count
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
    """Aggregate spending totals per category between ``start`` and ``end``.

    Default mode (``include_non_spending=False``):
        Sums ``SUM(-amount)`` over rows with ``amount < 0`` AND
        ``c.is_spending = TRUE`` (joining ``categories`` on the resolved
        category name). Non-spending categories (Transfers, Income, any
        user-added category with ``is_spending=FALSE``) are excluded.
        Spending categories come back with positive totals.

    ``include_non_spending=True``:
        Widens the filter to ``amount < 0 OR is_spending = FALSE``.
        Non-spending categories appear:
          - Income transactions are positive amounts, so ``SUM(-amount)``
            returns a NEGATIVE total — sign conveys "cash in."
          - Transfers transactions are negative amounts on the source
            account, so they appear with a POSITIVE total — but it's
            money moving to your own accounts, not spending.
        Spending categories are unchanged.

    Both modes group by category, sort by total descending.

    Refunds in spending categories (positive ``amount``, e.g. Dining
    refund) are NOT counted in either mode — the "amount < 0" filter
    enforces the literal "spending = money out" contract documented in
    the tool description. If dogfooding shows this matters for net-of-
    refunds analyses, a future ``include_refunds`` flag can be added.
    """
    rows = query_spending_by_category(store, start, end, include_non_spending=include_non_spending)
    return [{k: _serialize_value(v) for k, v in row.items()} for row in rows]
