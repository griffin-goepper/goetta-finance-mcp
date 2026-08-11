"""FastAPI factory for the local dashboard. Mirrors the ``build_server``
pattern from ``goetta_finance.server`` so the same store can be wired
into both the MCP tool surface and the dashboard.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from contextlib import AbstractAsyncContextManager
from importlib.resources import files
from pathlib import Path
from typing import Any

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from mcp.server.fastmcp import FastMCP
from starlette.datastructures import Headers
from starlette.responses import PlainTextResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from goetta_finance.store import FinanceStore
from goetta_finance.web.api import register_api
from goetta_finance.web.views import register_routes

# Host header values always accepted: this is how the user reaches their
# own dashboard.
_LOOPBACK_HOSTS: tuple[str, ...] = ("127.0.0.1", "localhost", "::1")

# Bind addresses meaning "every interface". We cannot enumerate the names
# that legitimately route to us, so the allowlist switches off and the CLI
# warns instead. Not a bind call — these are compared against --host.
_WILDCARD_BINDS = frozenset({"0.0.0.0", "::", "*"})  # noqa: S104  # nosec B104


def trusted_hosts_for(bind_host: str) -> tuple[str, ...]:
    """``Host`` header values to accept when uvicorn binds ``bind_host``.

    Loopback names are always included. A non-loopback bind adds its own
    literal, so ``--host 100.85.1.2`` keeps working over Tailscale. A
    wildcard bind returns ``("*",)`` — allowlist disabled, because there
    is no way to know which names reach us.
    """
    host = bind_host.strip().lower()
    if host in _WILDCARD_BINDS:
        return ("*",)
    return tuple(dict.fromkeys((*_LOOPBACK_HOSTS, host)))


def _hostname(host_header: str) -> str:
    """Hostname from a ``Host`` header, minus the port.

    Handles the IPv6 literal form (``[::1]:8765`` -> ``::1``), which is
    the case starlette's own middleware gets wrong.
    """
    value = host_header.strip().lower()
    if value.startswith("["):
        end = value.find("]")
        return value[1:end] if end != -1 else ""
    return value.split(":", 1)[0]


class HostAllowlistMiddleware:
    """Reject requests whose ``Host`` header is not in ``allowed_hosts``.

    This is the DNS-rebinding defense for the dashboard and ``/api/v1``.
    Binding to loopback is not protection on its own: a page the user
    visits can re-point its own domain at 127.0.0.1 once its DNS TTL
    expires, and from then on the browser treats this server as
    same-origin — so CORS never applies and every read-only endpoint is
    readable by that page. Pinning the ``Host`` header is what breaks it:
    the rebound request still arrives carrying the attacker's domain.

    ``/api/mcp`` already enforces this for itself (FastMCP's
    ``transport_security``, which checks ``Origin`` too, and answers 421
    — the status matched here). This closes the same hole on the
    surfaces we hand-rolled.

    Why not ``starlette.middleware.trustedhost.TrustedHostMiddleware``:
    it derives the hostname as ``host.split(":")[0]``, which yields
    ``"["`` for an IPv6 literal like ``[::1]:8765``. A ``--host ::1``
    bind could therefore never be allowlisted and would 400 on every
    request.
    """

    def __init__(self, app: ASGIApp, allowed_hosts: Sequence[str]) -> None:
        self.app = app
        self.allowed = {host.strip().lower() for host in allowed_hosts}
        self.allow_any = "*" in self.allowed

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if self.allow_any or scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return
        if _hostname(Headers(scope=scope).get("host", "")) in self.allowed:
            await self.app(scope, receive, send)
            return
        response = PlainTextResponse(
            "Invalid Host header. goetta-finance only answers on the address it "
            "was bound to; see --host.",
            status_code=421,
        )
        await response(scope, receive, send)


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
    allowed_hosts: Sequence[str] | None = None,
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

    ``allowed_hosts`` is the ``Host``-header allowlist (see
    :class:`HostAllowlistMiddleware`). Defaults to loopback names; callers
    that bind elsewhere should pass :func:`trusted_hosts_for` of their bind
    address. ``("*",)`` disables the check.

    Security posture (audited 2026-05 and 2026-08, see ``docs/SECURITY_AUDIT_*.md``):

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
    - **DNS rebinding** is blocked on every surface by
      ``HostAllowlistMiddleware``. ``/api/mcp`` additionally enforces its
      own ``Host``/``Origin`` check via FastMCP's ``transport_security``,
      which auto-enables for localhost binds
      (mcp.server.fastmcp.server:178-183). The 2026-08 audit found the
      hand-rolled surfaces had no such check while ``/api/mcp`` did —
      ``/api/v1/*`` served full 200s to a forged ``Host``. Do not remove
      the middleware on the grounds that the daemon "only binds to
      loopback": loopback is exactly what DNS rebinding targets.
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
    # Outermost middleware: a forged Host must not reach routing, a mounted
    # sub-app, or StaticFiles.
    app.add_middleware(
        HostAllowlistMiddleware,
        allowed_hosts=_LOOPBACK_HOSTS if allowed_hosts is None else allowed_hosts,
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
