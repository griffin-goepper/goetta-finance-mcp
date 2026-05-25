"""Plotly figure builders. Each function returns a ``{"data": ..., "layout": ...}``
dict (``Figure.to_dict()``) for the template to hand to ``Plotly.newPlot``.

We downcast ``Decimal`` to ``float`` here. The store keeps Decimals for
correctness; charts only need display precision.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, cast

import plotly.graph_objects as go

from goetta_finance.store import FinanceStore
from goetta_finance.web.aggregations import (
    monthly_income_spending,
    net_worth_series,
    spending_by_category_last_n_days,
)


def net_worth_figure(
    store: FinanceStore, *, days: int = 90, now: datetime | None = None
) -> dict[str, Any]:
    points = net_worth_series(store, days=days, now=now)
    figure = go.Figure(
        data=[
            go.Scatter(
                x=[p.day.isoformat() for p in points],
                y=[float(p.balance) for p in points],
                mode="lines+markers",
                name="Net worth",
                hovertemplate="%{x|%b %d, %Y}<br>$%{y:,.2f}<extra></extra>",
            )
        ],
        layout=go.Layout(
            title="Net worth",
            xaxis={"title": "Date"},
            yaxis={"title": "Balance (USD)", "tickformat": ",.0f"},
            margin={"l": 60, "r": 20, "t": 50, "b": 50},
        ),
    )
    return cast(dict[str, Any], figure.to_dict())


def spending_by_category_figure(
    store: FinanceStore, *, days: int = 30, now: datetime | None = None
) -> dict[str, Any]:
    """Pie chart of spending by category over the last ``days`` days.

    Uses Plotly's ``Pie`` trace which is bundled in ``plotly-basic.min.js``
    (verified in sub-seam 4 of the categorization slice). If the result is
    empty (no spending in the window) the figure still renders — Plotly
    handles empty pies gracefully — but the template shows a fallback
    message ahead of the chart for clarity.
    """
    rows = spending_by_category_last_n_days(store, days=days, now=now)
    figure = go.Figure(
        data=[
            go.Pie(
                labels=[r.category for r in rows],
                values=[float(r.total) for r in rows],
                hovertemplate="%{label}<br>$%{value:,.2f} (%{percent})<extra></extra>",
                textinfo="label+percent",
                sort=False,  # preserve descending-by-total order from SQL
            )
        ],
        layout=go.Layout(
            title=f"Spending by category — last {days} days",
            margin={"l": 20, "r": 20, "t": 50, "b": 20},
        ),
    )
    return cast(dict[str, Any], figure.to_dict())


def spending_figure(
    store: FinanceStore, *, months: int = 12, now: datetime | None = None
) -> dict[str, Any]:
    rows = monthly_income_spending(store, months=months, now=now)
    months_axis = [r.month.isoformat() for r in rows]
    figure = go.Figure(
        data=[
            go.Bar(
                x=months_axis,
                y=[float(r.income) for r in rows],
                name="Income",
                marker_color="#2ecc71",
                hovertemplate="%{x|%b %Y}<br>+$%{y:,.2f}<extra></extra>",
            ),
            go.Bar(
                x=months_axis,
                y=[-float(r.spending) for r in rows],
                name="Spending",
                marker_color="#e74c3c",
                hovertemplate="%{x|%b %Y}<br>-$%{customdata:,.2f}<extra></extra>",
                customdata=[float(r.spending) for r in rows],
            ),
        ],
        layout=go.Layout(
            title="Income and spending by month",
            barmode="relative",
            xaxis={"title": "Month", "type": "category"},
            yaxis={"title": "USD", "tickformat": ",.0f"},
            margin={"l": 60, "r": 20, "t": 50, "b": 50},
        ),
    )
    return cast(dict[str, Any], figure.to_dict())
