from __future__ import annotations

import json
import logging
import sys
from pathlib import Path
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
from goetta_finance.daemon import run_daemon
from goetta_finance.errors import GoettaFinanceError, SetupTokenError
from goetta_finance.mcp_config import (
    SERVER_KEY,
    build_http_server_entry,
    build_server_entry,
    claude_code_executable,
    claude_desktop_config_path,
    merge_into_config,
    register_with_claude_code,
    resolve_command,
    unregister_with_claude_code,
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
def daemon(
    host: Annotated[
        str, typer.Option("--host", help="Bind address. Default localhost-only.")
    ] = "127.0.0.1",
    port: Annotated[int, typer.Option("--port", help="HTTP port.")] = 8765,
    sync_at: Annotated[
        str,
        typer.Option(
            "--sync-at",
            help="HH:MM (24h) local time of the daily scheduled sync.",
        ),
    ] = "06:00",
    no_schedule: Annotated[
        bool,
        typer.Option(
            "--no-schedule",
            help="Disable the internal scheduler (manual sync only).",
        ),
    ] = False,
    no_mcp: Annotated[
        bool,
        typer.Option(
            "--no-mcp",
            help="Disable the MCP HTTP endpoint (dashboard + scheduler only).",
        ),
    ] = False,
) -> None:
    """Run the long-lived daemon: dashboard + MCP HTTP + scheduled sync.

    One process, one DuckDB write handle. The MCP endpoint is at
    ``http://<host>:<port>/api/mcp`` (register with
    ``claude mcp add goetta-finance --scope user --transport http <url>``).
    The dashboard is at ``http://<host>:<port>/``.
    """
    try:
        config = load_config()
        if not config.access_url:
            typer.secho(
                "No access URL configured. Run `goetta-finance init` first.",
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
        store = DuckDBStore(db_path(config))
        try:
            import duckdb

            try:
                _ = store.conn  # force-open to fail fast on a locked DB
            except duckdb.Error as exc:
                typer.secho(
                    f"Cannot open {db_path(config)}: {exc}",
                    fg=typer.colors.RED,
                    err=True,
                )
                typer.secho(
                    "Another goetta-finance process (serve, web, or daemon) "
                    "is already running and holds the DB lock. Stop it first.",
                    fg=typer.colors.YELLOW,
                    err=True,
                )
                raise typer.Exit(code=1) from exc
            store.init()
            client = SimpleFinClient(config.access_url)
            mcp_url = f"http://{host}:{port}/api/mcp" if not no_mcp else "(disabled)"
            typer.echo(f"goetta-finance daemon: http://{host}:{port}")
            typer.echo(f"  dashboard: http://{host}:{port}/")
            typer.echo(f"  MCP:       {mcp_url}")
            typer.echo(f"  schedule:  {'(disabled)' if no_schedule else sync_at + ' local'}")
            run_daemon(
                store,
                client,
                host=host,
                port=port,
                sync_at=sync_at,
                schedule_enabled=not no_schedule,
                mcp_enabled=not no_mcp,
            )
        finally:
            store.close()
    except GoettaFinanceError as exc:
        typer.secho(f"Daemon failed: {exc}", fg=typer.colors.RED, err=True)
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


_DAEMON_DEFAULT_HOST = "127.0.0.1"
_DAEMON_DEFAULT_PORT = 8765


def _daemon_mcp_url() -> str:
    return f"http://{_DAEMON_DEFAULT_HOST}:{_DAEMON_DEFAULT_PORT}/api/mcp"


def _daemon_health_url() -> str:
    return f"http://{_DAEMON_DEFAULT_HOST}:{_DAEMON_DEFAULT_PORT}/health"


def _poll_daemon_health(timeout_seconds: float = 5.0) -> bool:
    """Probe ``/health`` to see if a daemon is already running locally."""
    import time

    import httpx

    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        try:
            response = httpx.get(_daemon_health_url(), timeout=1.0)
            if response.status_code == 200:
                return True
        except httpx.HTTPError:
            pass
        time.sleep(0.25)
    return False


def _prompt_daemon_mode() -> bool:
    typer.echo("")
    typer.secho(
        "  Daemon mode: one long-lived process serves the dashboard AND the",
        bold=True,
    )
    typer.secho(
        "  MCP endpoint over HTTP. Recommended on Windows (otherwise the",
        bold=True,
    )
    typer.secho("  serve+web DuckDB lock conflict bites you).", bold=True)
    typer.echo("")
    typer.secho(
        "  NOTE: in v1 the daemon does NOT auto-start. You'll need to:",
        fg=typer.colors.YELLOW,
    )
    typer.secho(
        "    - keep `goetta-finance daemon` running in a separate terminal, OR",
        fg=typer.colors.YELLOW,
    )
    typer.secho(
        "    - install the systemd / launchd / Task Scheduler snippet from",
        fg=typer.colors.YELLOW,
    )
    typer.secho(
        '      README.md ("Scheduling") to start it at login.',
        fg=typer.colors.YELLOW,
    )
    typer.echo("")
    return typer.confirm("  Use daemon mode?", default=False)


def _run_init_mcp_step() -> None:
    command = resolve_command()
    use_daemon = _prompt_daemon_mode()
    if use_daemon:
        mcp_url = _daemon_mcp_url()
        typer.echo(f"  MCP URL: {mcp_url}")
    else:
        typer.echo(f"  Command to register: {command}")
    registered_any = False

    claude_code = claude_code_executable()
    if claude_code is not None:
        typer.echo(f"  Detected Claude Code: {claude_code}")
        if typer.confirm("  Register goetta-finance with Claude Code (user scope)?", default=True):
            if use_daemon:
                # Clear any stale stdio registration so we don't end up with
                # a dual-registration where one points at a stdio subprocess
                # we no longer expect to be invoked.
                for scope in ("user", "local"):
                    cleared, msg = unregister_with_claude_code(scope=scope)
                    if cleared and msg and "no existing" not in msg.lower():
                        typer.echo(f"  ✓ Cleared previous {scope}-scope registration.")
                ok, msg = register_with_claude_code(
                    command, transport="http", url=_daemon_mcp_url()
                )
            else:
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
                if use_daemon:
                    changed = _write_claude_desktop_http_entry(desktop_path, _daemon_mcp_url())
                else:
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
        _print_manual_snippet(command, http_url=_daemon_mcp_url() if use_daemon else None)

    if use_daemon:
        typer.echo("")
        if _poll_daemon_health(timeout_seconds=2.0):
            typer.secho("  ✓ Daemon is already running.", fg=typer.colors.GREEN)
        else:
            typer.secho(
                "  Daemon is not running yet. Start it now:",
                fg=typer.colors.YELLOW,
            )
            typer.echo("    goetta-finance daemon")
            typer.echo("  Then restart Claude.")


def _print_manual_snippet(command: str, *, http_url: str | None = None) -> None:
    entry = build_http_server_entry(http_url) if http_url else build_server_entry(command)
    snippet = {"mcpServers": {SERVER_KEY: entry}}
    typer.echo("  Add to your Claude Desktop config manually:")
    typer.echo("")
    typer.echo(json.dumps(snippet, indent=2))


def _write_claude_desktop_http_entry(path: Path, url: str) -> bool:
    """Merge the daemon-mode HTTP entry into Claude Desktop's config.

    Mirrors ``write_claude_desktop_config`` but uses the HTTP entry
    shape. Atomic-replace via tmp-file as in the stdio writer.
    """
    import os as _os

    from goetta_finance.errors import ConfigError

    path.parent.mkdir(parents=True, exist_ok=True)
    existing: dict[str, object] = {}
    if path.exists():
        try:
            with path.open("r", encoding="utf-8") as f:
                loaded = json.load(f)
        except (OSError, json.JSONDecodeError) as exc:
            raise ConfigError(
                f"Existing Claude Desktop config at {path} is unreadable: {exc}"
            ) from exc
        if not isinstance(loaded, dict):
            raise ConfigError(f"Existing Claude Desktop config at {path} is not a JSON object.")
        existing = loaded

    merged, changed = merge_into_config(existing, build_http_server_entry(url))
    if not changed:
        return False

    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(merged, f, indent=2, sort_keys=True)
        f.write("\n")
    _os.replace(tmp, path)
    return True


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
