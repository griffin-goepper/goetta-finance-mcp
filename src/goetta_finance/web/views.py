"""Route handlers for the local dashboard.

The dashboard is read-only by design: ``app.state.store`` is the
``FinanceStore`` opened with ``read_only=True``. Handlers never mutate.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from decimal import Decimal
from typing import Annotated, Any, cast

from fastapi import FastAPI, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse

from goetta_finance.tools.accounts import serialize_account
from goetta_finance.tools.transactions import serialize_transaction
from goetta_finance.web.aggregations import recent_sync_runs
from goetta_finance.web.charts import net_worth_figure, spending_figure


def _store(request: Request) -> Any:
    return request.app.state.store


def _render(request: Request, template: str, context: dict[str, Any]) -> HTMLResponse:
    templates = request.app.state.templates
    return cast(HTMLResponse, templates.TemplateResponse(request, template, context))


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt


def register_routes(app: FastAPI) -> None:
    @app.get("/health", response_class=JSONResponse)
    async def health(request: Request) -> JSONResponse:
        """Daemon-readiness probe. Used by the ``init`` wizard to confirm
        a running daemon before writing the Claude Code HTTP registration,
        and as a generic liveness check from cron-style monitors.
        """
        store = _store(request)
        last = store.last_sync_time()
        try:
            accounts_count = len(store.get_accounts())
        except Exception:
            accounts_count = None
        return JSONResponse(
            {
                "ok": True,
                "last_sync": last.isoformat() if last else None,
                "accounts": accounts_count,
                "mcp_enabled": request.app.state.mcp_server is not None,
            }
        )

    @app.get("/", response_class=HTMLResponse)
    async def index(request: Request) -> HTMLResponse:
        store = _store(request)
        accounts = [serialize_account(a) for a in store.get_accounts()]
        return _render(
            request,
            "accounts.html",
            {"accounts": accounts, "active": "accounts"},
        )

    @app.get("/net-worth", response_class=HTMLResponse)
    async def net_worth(request: Request, days: int = 90) -> HTMLResponse:
        store = _store(request)
        figure = net_worth_figure(store, days=days)
        return _render(
            request,
            "net_worth.html",
            {
                "figure_data": json.dumps(figure["data"], default=_json_default),
                "figure_layout": json.dumps(figure["layout"], default=_json_default),
                "days": days,
                "active": "net_worth",
            },
        )

    @app.get("/spending", response_class=HTMLResponse)
    async def spending(request: Request, months: int = 12) -> HTMLResponse:
        store = _store(request)
        figure = spending_figure(store, months=months)
        return _render(
            request,
            "spending.html",
            {
                "figure_data": json.dumps(figure["data"], default=_json_default),
                "figure_layout": json.dumps(figure["layout"], default=_json_default),
                "months": months,
                "active": "spending",
            },
        )

    @app.get("/transactions", response_class=HTMLResponse)
    async def transactions(
        request: Request,
        account_id: Annotated[str | None, Query()] = None,
        start: Annotated[str | None, Query()] = None,
        end: Annotated[str | None, Query()] = None,
        q: Annotated[str | None, Query()] = None,
        limit: int = 200,
    ) -> HTMLResponse:
        store = _store(request)
        accounts = [serialize_account(a) for a in store.get_accounts()]
        rows = _query_transactions(store, account_id, start, end, q, limit)
        return _render(
            request,
            "transactions.html",
            {
                "accounts": accounts,
                "rows": rows,
                "filters": {
                    "account_id": account_id or "",
                    "start": start or "",
                    "end": end or "",
                    "q": q or "",
                },
                "active": "transactions",
            },
        )

    @app.get("/transactions/rows", response_class=HTMLResponse)
    async def transactions_rows(
        request: Request,
        account_id: Annotated[str | None, Query()] = None,
        start: Annotated[str | None, Query()] = None,
        end: Annotated[str | None, Query()] = None,
        q: Annotated[str | None, Query()] = None,
        limit: int = 200,
    ) -> HTMLResponse:
        store = _store(request)
        rows = _query_transactions(store, account_id, start, end, q, limit)
        return _render(request, "partials/transactions_table.html", {"rows": rows})

    @app.get("/sync", response_class=HTMLResponse)
    async def sync_health(request: Request) -> HTMLResponse:
        store = _store(request)
        last = store.last_sync_time()
        runs = recent_sync_runs(store, limit=10)
        for r in runs:
            r["warnings_list"] = _maybe_json_list(r.get("warnings"))
            r["errors_list"] = _maybe_json_list(r.get("errors"))
        return _render(
            request,
            "sync_health.html",
            {
                "last_sync_local": last.astimezone() if last else None,
                "runs": runs,
                "active": "sync",
            },
        )


def _query_transactions(
    store: Any,
    account_id: str | None,
    start: str | None,
    end: str | None,
    q: str | None,
    limit: int,
) -> list[dict[str, Any]]:
    txns = store.get_transactions(
        account_id=account_id or None,
        start=_parse_iso(start),
        end=_parse_iso(end),
        limit=max(1, min(limit, 1000)),
    )
    if q:
        needle = q.lower()
        txns = [
            t
            for t in txns
            if needle in t.description.lower()
            or (t.payee is not None and needle in t.payee.lower())
        ]
    return [serialize_transaction(t) for t in txns]


def _maybe_json_list(value: Any) -> list[str]:
    if value in (None, "", "null"):
        return []
    if isinstance(value, list):
        return [str(v) for v in value]
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return [value]
        if isinstance(parsed, list):
            return [str(v) for v in parsed]
        return [str(parsed)]
    return [str(value)]


def _json_default(value: Any) -> Any:
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, datetime):
        return value.isoformat()
    raise TypeError(f"unserializable: {type(value).__name__}")
