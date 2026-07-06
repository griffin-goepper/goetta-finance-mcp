"""Read-only JSON API (``/api/v1``) for companion frontends.

Every endpoint is GET-only and reads through the same ``FinanceStore``
as the HTML dashboard — no write surface, no CORS (same-origin by
design; see the security-posture docstring on ``build_app``). Money is
emitted as strings and timestamps as ISO 8601, matching the MCP tool
serialization convention (``tools/_serialize.py``).

Ordering invariant: ``register_api`` must run BEFORE the ``/api`` MCP
mount in ``build_app`` — Starlette matches routes in registration
order, so the exact ``/api/v1/*`` routes only win over the ``/api``
sub-app mount if they are registered first. Pinned by
``test_api_routes_win_over_mcp_mount``.

Handlers are sync ``def`` on purpose: FastAPI runs them in a thread
pool, so blocking DuckDB calls never stall the event loop the daemon's
MCP transport and scheduler run on.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Annotated, Any

from fastapi import APIRouter, FastAPI, HTTPException, Query, Request

from goetta_finance.goals import spending_cap_history
from goetta_finance.models import GoalKind
from goetta_finance.store import FinanceStore
from goetta_finance.tools._serialize import serialize_value
from goetta_finance.tools.accounts import list_accounts
from goetta_finance.tools.goals import list_goals
from goetta_finance.tools.spending_by_category import query_spending_by_category
from goetta_finance.tools.transactions import get_transactions
from goetta_finance.web.aggregations import (
    display_currency,
    monthly_income_spending,
    monthly_spending_by_category,
    net_worth_series,
    parse_json_list,
    recent_sync_runs,
)


def _store(request: Request) -> FinanceStore:
    store: FinanceStore = request.app.state.store
    return store


def _parse_iso(value: str | None) -> datetime | None:
    """ISO parse for query params; naive values are taken as UTC.

    Unlike the HTML dashboard's forgiving filter parse, a malformed value
    here is a 400 — an API client that sent an explicit date must not be
    silently served the default window (classic trap: an unencoded ``+``
    in a ``+00:00`` offset decodes to a space and would otherwise fall
    through). Absent/empty stays None.
    """
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=f"invalid ISO datetime: {value!r}") from exc
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt


def _clamp(value: int, lo: int, hi: int) -> int:
    return max(lo, min(value, hi))


def _months_back_start(now: datetime, months: int) -> datetime:
    """First instant of the (months-1)th-prior UTC calendar month —
    the same window computation the monthly aggregations use."""
    year = now.year
    month = now.month - (months - 1)
    while month <= 0:
        month += 12
        year -= 1
    return datetime(year, month, 1, tzinfo=UTC)


def _signed(balance: Decimal, *, is_liability: bool) -> Decimal:
    """Signed-balance formula shared with the dashboard and
    SQL_SCHEMA_HINT: liabilities contribute ``-ABS(balance)``."""
    return -abs(balance) if is_liability else balance


def register_api(app: FastAPI) -> None:
    """Attach the ``/api/v1`` router. Call before the ``/api`` MCP mount."""
    router = APIRouter(prefix="/api/v1")

    @router.get("/summary")
    def summary(request: Request) -> dict[str, Any]:
        store = _store(request)
        visible = store.get_accounts()
        hidden = [a for a in store.get_accounts(include_hidden=True) if a.is_hidden]
        net_worth = sum(
            (_signed(a.balance, is_liability=a.is_liability) for a in visible), Decimal("0")
        )
        hidden_total = sum(
            (_signed(a.balance, is_liability=a.is_liability) for a in hidden), Decimal("0")
        )
        last = store.last_sync_time()
        return {
            "net_worth": str(net_worth),
            "currency": display_currency(store),
            "accounts_count": len(visible),
            "hidden_count": len(hidden),
            "hidden_total": str(hidden_total),
            "last_sync": last.isoformat() if last else None,
        }

    @router.get("/accounts")
    def accounts(request: Request, include_hidden: bool = False) -> dict[str, Any]:
        return {"accounts": list_accounts(_store(request), include_hidden=include_hidden)}

    @router.get("/net-worth")
    def net_worth(request: Request, days: int = 90) -> dict[str, Any]:
        days = _clamp(days, 1, 1830)
        points = net_worth_series(_store(request), days=days)
        return {
            "days": days,
            "points": [{"date": p.day.isoformat(), "balance": str(p.balance)} for p in points],
        }

    @router.get("/cashflow/monthly")
    def cashflow_monthly(request: Request, months: int = 12) -> dict[str, Any]:
        months = _clamp(months, 1, 60)
        rows = monthly_income_spending(_store(request), months=months)
        return {
            "months": months,
            "rows": [
                {
                    "month": r.month.isoformat(),
                    "income": str(r.income),
                    "spending": str(r.spending),
                }
                for r in rows
            ],
        }

    @router.get("/spending/by-category")
    def spending_by_category(
        request: Request,
        days: int = 30,
        start: Annotated[str | None, Query()] = None,
        end: Annotated[str | None, Query()] = None,
    ) -> dict[str, Any]:
        """Net spending per category over a window. An explicit
        ``start``/``end`` wins over ``days``; a missing side defaults to
        ``end=now`` / ``start=end-days``."""
        store = _store(request)
        days = _clamp(days, 1, 1830)
        end_dt = _parse_iso(end) or datetime.now(tz=UTC)
        start_dt = _parse_iso(start) or end_dt - timedelta(days=days)
        colors = {c.name: c.display_color for c in store.get_categories()}
        rows = query_spending_by_category(store, start_dt, end_dt)
        return {
            "start": start_dt.isoformat(),
            "end": end_dt.isoformat(),
            "rows": [
                {
                    "category": row["category"],
                    "total": serialize_value(row["total"]),
                    "transaction_count": row["transaction_count"],
                    "color": colors.get(str(row["category"])),
                }
                for row in rows
            ],
        }

    @router.get("/spending/by-month")
    def spending_by_month(
        request: Request,
        months: int = 12,
        category: Annotated[str | None, Query()] = None,
    ) -> dict[str, Any]:
        """Long-format month x category net-spending matrix. Sparse —
        callers zero-fill their own buckets. Pending transactions count,
        matching the spending-cap goal semantics (NOT the pending-excluded
        ``/cashflow/monthly`` bars)."""
        months = _clamp(months, 1, 60)
        rows = monthly_spending_by_category(_store(request), months=months, category=category)
        return {
            "months": months,
            "rows": [
                {
                    "month": r.month.isoformat(),
                    "category": r.category,
                    "total": str(r.total),
                    "transaction_count": r.transaction_count,
                }
                for r in rows
            ],
        }

    @router.get("/goals")
    def goals(request: Request) -> dict[str, Any]:
        # Byte-for-byte the MCP list_goals serialization — one home for
        # goal math AND for its JSON shape.
        return {"goals": list_goals(_store(request))}

    @router.get("/goals/{goal_id}/history")
    def goal_history(request: Request, goal_id: int, periods: int = 12) -> dict[str, Any]:
        """Historical actuals for one goal.

        Spending caps: per-period buckets at the goal's own granularity
        (month/year), oldest first; the newest bucket equals the goal
        card's ``current`` to the cent. Balance goals: raw balance
        snapshots (``abs()`` for liabilities — amount owed), ``periods``
        read as calendar months of lookback.
        """
        store = _store(request)
        periods = _clamp(periods, 1, 36)
        goal = next((g for g in store.list_goals() if g.id == goal_id), None)
        if goal is None:
            raise HTTPException(status_code=404, detail=f"goal not found: {goal_id}")
        if goal.kind is GoalKind.SPENDING_CAP:
            buckets = spending_cap_history(store, goal, periods=periods)
            return {
                "goal_id": goal.id,
                "kind": goal.kind.value,
                "period": goal.period.value if goal.period is not None else None,
                "category": goal.category_name,
                "target": str(goal.amount),
                "periods": [
                    {
                        "period_start": b.period_start.isoformat(),
                        "period_end": b.period_end.isoformat(),
                        "actual": str(b.actual),
                        "over": b.over,
                    }
                    for b in buckets
                ],
            }
        if goal.account_id is None:  # pragma: no cover - table CHECK
            raise HTTPException(status_code=404, detail=f"goal {goal_id} has no account")
        account = next(
            (a for a in store.get_accounts(include_hidden=True) if a.id == goal.account_id),
            None,
        )
        if account is None:  # pragma: no cover - FK + delete_account guard
            raise HTTPException(status_code=404, detail=f"account not found: {goal.account_id}")
        now = datetime.now(tz=UTC)
        snapshots = store.get_balance_history(
            goal.account_id, since=_months_back_start(now, periods)
        )
        return {
            "goal_id": goal.id,
            "kind": goal.kind.value,
            "account_id": goal.account_id,
            "account_name": goal.account_name,
            "target": str(goal.amount),
            "direction": goal.direction.value if goal.direction is not None else None,
            "target_date": serialize_value(goal.target_date),
            "points": [
                {
                    "timestamp": s.timestamp.isoformat(),
                    "value": str(abs(s.balance) if account.is_liability else s.balance),
                }
                for s in snapshots
            ],
        }

    @router.get("/transactions")
    def transactions(
        request: Request,
        account_id: Annotated[str | None, Query()] = None,
        start: Annotated[str | None, Query()] = None,
        end: Annotated[str | None, Query()] = None,
        category: Annotated[str | None, Query()] = None,
        q: Annotated[str | None, Query()] = None,
        limit: int = 100,
    ) -> dict[str, Any]:
        rows = get_transactions(
            _store(request),
            account_id=account_id or None,
            start=_parse_iso(start),
            end=_parse_iso(end),
            category=category or None,
            search=q or None,
            limit=_clamp(limit, 1, 1000),
        )
        return {"transactions": rows, "count": len(rows)}

    @router.get("/categories")
    def categories(request: Request) -> dict[str, Any]:
        return {
            "categories": [
                {
                    "id": c.id,
                    "name": c.name,
                    "color": c.display_color,
                    "is_spending": c.is_spending,
                }
                for c in _store(request).get_categories()
            ]
        }

    @router.get("/sync/status")
    def sync_status(request: Request, limit: int = 10) -> dict[str, Any]:
        store = _store(request)
        last = store.last_sync_time()
        runs = recent_sync_runs(store, limit=_clamp(limit, 1, 100))
        return {
            "last_sync": last.isoformat() if last else None,
            "runs": [
                {
                    "id": r["id"],
                    "started_at": serialize_value(r["started_at"]),
                    "finished_at": serialize_value(r["finished_at"]),
                    "accounts_touched": r["accounts_touched"],
                    "transactions_new": r["transactions_new"],
                    "transactions_updated": r["transactions_updated"],
                    "warnings": parse_json_list(r.get("warnings")),
                    "errors": parse_json_list(r.get("errors")),
                }
                for r in runs
            ],
        }

    app.include_router(router)
