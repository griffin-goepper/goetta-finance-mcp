"""FastAPI factory for the local dashboard. Mirrors the ``build_server``
pattern from ``goetta_finance.server`` so the same store can be wired
into both the MCP tool surface and the dashboard.
"""

from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from importlib.resources import files
from pathlib import Path
from typing import Any

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from mcp.server.fastmcp import FastMCP

from goetta_finance.store import FinanceStore
from goetta_finance.web.api import register_api
from goetta_finance.web.views import register_routes


def _templates_path() -> Path:
    return Path(str(files("goetta_finance.web").joinpath("templates")))


def _static_path() -> Path:
    return Path(str(files("goetta_finance.web").joinpath("static")))


def build_app(
    store: FinanceStore,
    *,
    title: str = "goetta-finance",
    mcp_server: FastMCP | None = None,
    lifespan: Callable[[FastAPI], AbstractAsyncContextManager[Any]] | None = None,
    dash_dir: Path | None = None,
) -> FastAPI:
    """Construct the dashboard FastAPI app.

    ``mcp_server`` (daemon mode) mounts the streamable-HTTP MCP transport at
    ``/api/mcp`` (FastMCP exposes itself at ``/mcp`` internally, mounted
    under ``/api``). Pass ``None`` for the dashboard-only ``web`` command
    where MCP runs separately over stdio.

    ``lifespan`` is the FastAPI lifespan context manager — daemon mode uses
    it to run the scheduler loop and ensure clean cancellation on shutdown.

    ``dash_dir`` (opt-in) mounts a user-supplied static single-page-app
    build at ``/dash`` — the supported way to serve a companion frontend
    same-origin so it can call ``/api/v1`` without CORS. The CLI validates
    the directory contains an ``index.html``.

    Security posture (audited 2026-05, see ``docs/SECURITY_AUDIT_2026-05.md``):

    - **No CORS middleware by design.** The dashboard is meant to be hit
      same-origin from the user's own browser at ``http://127.0.0.1:8765``.
      Permissive CORS headers would expose every read-only endpoint to
      malicious websites the user happens to visit. If a future contributor
      adds CORS "for testing", that needs explicit threat-model review.
      Companion frontends get the same-origin ``/dash`` mount instead.
    - **``/api/v1``** (``web/api.py``) is a GET-only, read-only JSON
      surface over the same store — no write endpoints, no auth (identical
      posture to the HTML pages: whoever can reach the port can read).
      Binding beyond localhost remains user-opt-in with the CLI warning.
    - **DNS rebinding** on the ``/api/mcp`` sub-app is handled by FastMCP's
      built-in ``transport_security`` middleware, which auto-enables for
      localhost binds with allowed_hosts/origins restricted to
      127.0.0.1 / localhost / ::1 (mcp.server.fastmcp.server:178-183).
    - **CSRF** is not enforced because every dashboard route is a GET; the
      only POST surface is ``/api/mcp``, which is protected by the
      transport_security middleware above.
    """
    app = FastAPI(
        title=title,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=lifespan,
    )
    templates = Jinja2Templates(directory=_templates_path())
    app.state.store = store
    app.state.templates = templates
    app.state.mcp_server = mcp_server  # daemon and tests introspect this
    app.mount("/static", StaticFiles(directory=_static_path()), name="static")
    register_routes(app)
    register_api(app)
    if dash_dir is not None:
        # html=True serves index.html at /dash/; the SPA must use hash
        # routing (unknown deep paths 404 — StaticFiles has no fallback).
        app.mount("/dash", StaticFiles(directory=dash_dir, html=True), name="dash")
    if mcp_server is not None:
        # FastMCP's streamable_http_app() is a Starlette ASGI app exposing
        # ``/mcp``. Mount at ``/api`` so the full URL is ``/api/mcp`` —
        # avoids collision with the dashboard's ``/`` route. ORDERING
        # INVARIANT: this mount must come AFTER register_api — Starlette
        # matches in registration order, so the exact /api/v1/* routes
        # would be swallowed by this sub-app if it were mounted first
        # (pinned by test_api_routes_win_over_mcp_mount).
        app.mount("/api", mcp_server.streamable_http_app())
    return app
