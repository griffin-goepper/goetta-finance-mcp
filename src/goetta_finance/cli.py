from __future__ import annotations

import json
import logging
import sys
from typing import Annotated

import typer

from goetta_finance import __version__
from goetta_finance.collector import collect
from goetta_finance.config import (
    config_path,
    db_path,
    home_dir,
    load_config,
    save_config,
)
from goetta_finance.errors import GoettaFinanceError, SetupTokenError
from goetta_finance.mcp_config import (
    SERVER_KEY,
    build_server_entry,
    claude_code_executable,
    claude_desktop_config_path,
    register_with_claude_code,
    resolve_command,
    write_claude_desktop_config,
)
from goetta_finance.server import build_server
from goetta_finance.simplefin import SimpleFinClient
from goetta_finance.store.duckdb_store import DuckDBStore

app = typer.Typer(
    help="Local-first MCP server that connects SimpleFIN to Claude.",
    add_completion=False,
    no_args_is_help=True,
)


def _configure_logging(verbose: bool) -> None:
    level = logging.DEBUG if verbose else logging.WARNING
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )


@app.callback()
def main(
    verbose: Annotated[bool, typer.Option("--verbose", "-v", help="Enable debug logging.")] = False,
) -> None:
    _configure_logging(verbose)


@app.command()
def init() -> None:
    """Interactive setup. Re-runnable; each step is skipped if already done."""
    try:
        _run_init()
    except GoettaFinanceError as exc:
        typer.secho(f"Setup failed: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1) from exc


@app.command()
def sync() -> None:
    """Pull fresh data from SimpleFIN."""
    try:
        config = load_config()
        if not config.access_url:
            typer.secho(
                "No access URL configured. Run `goetta-finance init` first.",
                fg=typer.colors.RED,
                err=True,
            )
            raise typer.Exit(code=1)
        store = DuckDBStore(db_path(config))
        store.init()
        try:
            client = SimpleFinClient(config.access_url)
            run = collect(store, client)
        finally:
            store.close()
        typer.echo(
            f"Synced: {run.transactions_new} new, "
            f"{run.transactions_updated} updated, "
            f"{run.accounts_touched} accounts."
        )
        for warning in run.warnings:
            typer.secho(f"  warning: {warning}", fg=typer.colors.YELLOW)
    except GoettaFinanceError as exc:
        typer.secho(f"Sync failed: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1) from exc


@app.command()
def serve() -> None:
    """Start the MCP server over stdio (long-lived, used by MCP clients)."""
    try:
        config = load_config()
        if not config.access_url:
            typer.secho(
                "No access URL configured. Run `goetta-finance init` first.",
                fg=typer.colors.RED,
                err=True,
            )
            raise typer.Exit(code=1)
        store = DuckDBStore(db_path(config))
        store.init()
        client = SimpleFinClient(config.access_url)
        try:
            mcp = build_server(store, client=client)
            mcp.run(transport="stdio")
        finally:
            store.close()
    except GoettaFinanceError as exc:
        typer.secho(f"Serve failed: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1) from exc


@app.command()
def web(
    host: Annotated[
        str, typer.Option("--host", help="Bind address. Default localhost-only.")
    ] = "127.0.0.1",
    port: Annotated[int, typer.Option("--port", help="HTTP port.")] = 8765,
) -> None:
    """Start the local web dashboard at http://<host>:<port>."""
    try:
        config = load_config()
        target_db = db_path(config)
        if not target_db.exists():
            typer.secho(
                f"No DuckDB store at {target_db}. Run `goetta-finance init` first.",
                fg=typer.colors.RED,
                err=True,
            )
            raise typer.Exit(code=1)
        if host != "127.0.0.1" and host != "localhost":
            typer.secho(
                f"WARNING: binding to {host}; anyone on this network can read "
                f"your finances. No auth is enforced.",
                fg=typer.colors.YELLOW,
                err=True,
            )
        store = DuckDBStore(target_db, read_only=True)
        try:
            import duckdb

            try:
                _ = store.conn  # force-open so a locked DB fails fast
            except duckdb.Error as exc:
                typer.secho(
                    f"Cannot open {target_db}: {exc}",
                    fg=typer.colors.RED,
                    err=True,
                )
                typer.secho(
                    "If `goetta-finance serve` (MCP) or another writer is "
                    "running, stop it first. DuckDB holds an exclusive file "
                    "lock on Windows even for a read-only handle.",
                    fg=typer.colors.YELLOW,
                    err=True,
                )
                raise typer.Exit(code=1) from exc

            from goetta_finance.web.app import build_app

            web_app = build_app(store)
            import uvicorn

            typer.echo(f"goetta-finance dashboard at http://{host}:{port}")
            uvicorn.run(web_app, host=host, port=port, log_level="warning")
        finally:
            store.close()
    except GoettaFinanceError as exc:
        typer.secho(f"Web failed: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1) from exc


@app.command()
def status() -> None:
    """Show sync health and current balances."""
    try:
        config = load_config()
        if not config.access_url:
            typer.echo("Not configured yet. Run `goetta-finance init` to get started.")
            return
        store = DuckDBStore(db_path(config))
        store.init()
        try:
            _print_status(store)
        finally:
            store.close()
    except GoettaFinanceError as exc:
        typer.secho(f"Status failed: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1) from exc


def _run_init() -> None:
    typer.echo(f"goetta-finance {__version__} — interactive setup")
    typer.echo(f"Config home: {home_dir()}")
    typer.echo("")

    config = load_config()

    # [1/4] SimpleFIN
    typer.secho("[1/4] SimpleFIN account", bold=True)
    if config.access_url:
        typer.echo("  ✓ Access URL already configured.")
        if typer.confirm("  Replace it?", default=False):
            config.access_url = _prompt_setup_token_and_claim()
    else:
        config.access_url = _prompt_setup_token_and_claim()
    save_config(config)
    typer.echo(f"  ✓ Saved access URL to {config_path()}")
    typer.echo("")

    # [2/4] Storage
    typer.secho("[2/4] Storage backend", bold=True)
    typer.echo(f"  Backend: {config.backend} (only option in Phase 1)")
    store = DuckDBStore(db_path(config))
    store.init()
    typer.echo(f"  ✓ Initialized DuckDB at {db_path(config)}")
    typer.echo("")

    # [3/4] Initial data pull
    typer.secho("[3/4] Initial data pull", bold=True)
    if typer.confirm("  Pull data now?", default=True):
        client = SimpleFinClient(config.access_url)
        run = collect(store, client)
        typer.echo(
            f"  ✓ {run.accounts_touched} accounts, {run.transactions_new} transactions imported."
        )
        for warning in run.warnings:
            typer.secho(f"  ⚠ {warning}", fg=typer.colors.YELLOW)
    else:
        typer.echo("  Skipped. Run `goetta-finance sync` when ready.")
    store.close()
    typer.echo("")

    # [4/4] MCP client integration
    typer.secho("[4/4] MCP client integration", bold=True)
    _run_init_mcp_step()
    typer.echo("")
    typer.secho("Setup complete.", fg=typer.colors.GREEN, bold=True)
    typer.echo("Run `goetta-finance status` any time to check sync health.")


def _run_init_mcp_step() -> None:
    command = resolve_command()
    typer.echo(f"  Command to register: {command}")
    registered_any = False

    claude_code = claude_code_executable()
    if claude_code is not None:
        typer.echo(f"  Detected Claude Code: {claude_code}")
        if typer.confirm("  Register goetta-finance with Claude Code (user scope)?", default=True):
            ok, msg = register_with_claude_code(command)
            if ok:
                typer.echo(f"  ✓ Registered with Claude Code. {msg}".rstrip())
                typer.echo(
                    "    Start a new `claude` session and the goetta-finance tools "
                    "will be available."
                )
                registered_any = True
            else:
                typer.secho(f"  Claude Code registration failed: {msg}", fg=typer.colors.YELLOW)
                typer.echo(
                    f"    If already registered, run "
                    f"`claude mcp remove {SERVER_KEY}` and rerun init."
                )

    desktop_path = claude_desktop_config_path()
    if desktop_path is not None:
        typer.echo(f"  Detected Claude Desktop config: {desktop_path}")
        if typer.confirm("  Write goetta-finance into Claude Desktop's config?", default=True):
            try:
                changed = write_claude_desktop_config(desktop_path, command=command)
            except GoettaFinanceError as exc:
                typer.secho(f"  {exc}", fg=typer.colors.RED)
            else:
                if changed:
                    typer.echo(f"  ✓ Wrote {SERVER_KEY} entry to {desktop_path}")
                    typer.echo("    Fully quit Claude Desktop (system-tray Quit) and reopen.")
                else:
                    typer.echo(f"  ✓ {SERVER_KEY} entry already up to date.")
                registered_any = True
                typer.secho(
                    "    Note: the Microsoft Store build of Claude Desktop reads "
                    "config from a sandboxed path our wizard doesn't yet target. "
                    "If the tools don't appear after restart, use Claude Code or "
                    "the direct-download Claude Desktop from claude.ai/download.",
                    fg=typer.colors.YELLOW,
                )

    if not registered_any:
        _print_manual_snippet(command)


def _print_manual_snippet(command: str) -> None:
    snippet = {"mcpServers": {SERVER_KEY: build_server_entry(command)}}
    typer.echo("  Add to your Claude Desktop config manually:")
    typer.echo("")
    typer.echo(json.dumps(snippet, indent=2))


def _prompt_setup_token_and_claim() -> str:
    typer.echo("  Get a setup token at https://bridge.simplefin.org/")
    while True:
        token = typer.prompt("  Setup token", hide_input=False).strip()
        if not token:
            typer.secho("  Token cannot be empty.", fg=typer.colors.RED)
            continue
        try:
            access_url = SimpleFinClient.claim(token)
            typer.echo("  ✓ Claimed access URL.")
            return access_url
        except SetupTokenError as exc:
            typer.secho(f"  {exc}", fg=typer.colors.RED)
            if not typer.confirm("  Try a different token?", default=True):
                raise


def _print_status(store: DuckDBStore) -> None:
    accounts = store.get_accounts()
    last = store.last_sync_time()
    typer.echo(f"Accounts: {len(accounts)}")
    for a in accounts:
        org = a.org_name or "—"
        typer.echo(f"  [{org}] {a.name}: {a.balance:.2f} {a.currency}")

    if last is None:
        typer.echo("\nNo successful syncs yet.")
    else:
        local = last.astimezone()
        typer.echo(f"\nLast sync: {local.isoformat(timespec='seconds')}")

    latest = store.conn.execute(
        """
        SELECT transactions_new, transactions_updated, warnings, errors
        FROM sync_runs
        ORDER BY id DESC
        LIMIT 1
        """
    ).fetchone()
    if latest is not None:
        new, updated, warnings, errors = latest
        typer.echo(f"  Last run: {new} new, {updated} updated.")
        _print_json_list("warning", warnings, typer.colors.YELLOW)
        _print_json_list("error", errors, typer.colors.RED)


def _print_json_list(label: str, value: object, color: str) -> None:
    if value in (None, "", "null"):
        return
    if isinstance(value, str):
        try:
            items = json.loads(value)
        except json.JSONDecodeError:
            return
    elif isinstance(value, list):
        items = value
    else:
        return
    for item in items:
        typer.secho(f"  {label}: {item}", fg=color)


if __name__ == "__main__":  # pragma: no cover
    sys.exit(app())
