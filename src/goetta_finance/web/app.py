"""FastAPI factory for the local dashboard. Mirrors the ``build_server``
pattern from ``goetta_finance.server`` so the same store can be wired
into both the MCP tool surface and the dashboard.
"""

from __future__ import annotations

from importlib.resources import files
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from goetta_finance.store import FinanceStore
from goetta_finance.web.views import register_routes


def _templates_path() -> Path:
    return Path(str(files("goetta_finance.web").joinpath("templates")))


def _static_path() -> Path:
    return Path(str(files("goetta_finance.web").joinpath("static")))


def build_app(store: FinanceStore, *, title: str = "goetta-finance") -> FastAPI:
    app = FastAPI(title=title, docs_url=None, redoc_url=None, openapi_url=None)
    templates = Jinja2Templates(directory=_templates_path())
    app.state.store = store
    app.state.templates = templates
    app.mount("/static", StaticFiles(directory=_static_path()), name="static")
    register_routes(app)
    return app
