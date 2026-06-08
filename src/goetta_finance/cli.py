from __future__ import annotations

import difflib
import json
import logging
import re
import sys
import uuid
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
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
    write_default_prefixes_file,
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
from goetta_finance.models import Account, AccountType, BalanceSnapshot
from goetta_finance.server import build_server
from goetta_finance.simplefin import SimpleFinClient
from goetta_finance.store.duckdb_store import DuckDBStore
from goetta_finance.validators import (
    RulePatternError,
    parse_match_type,
    validate_rule_pattern,
)

MANUAL_ID_PREFIX = "MANUAL-"

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
    prefixes_file = write_default_prefixes_file()
    typer.echo(f"  ✓ Prefix-strip list at {prefixes_file} (edit to match your bank)")
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
    # Status is for the human — show hidden accounts too, tagged. They
    # still own the money; hiding only affects default read paths.
    accounts = store.get_accounts(include_hidden=True)
    last = store.last_sync_time()
    typer.echo(f"Accounts: {len(accounts)}")
    for a in accounts:
        org = a.org_name or "—"
        hidden_tag = " [hidden]" if a.is_hidden else ""
        typer.echo(f"  [{org}] {a.name}{hidden_tag}: {a.balance:.2f} {a.currency}")

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


account_app = typer.Typer(
    help="Manage manual accounts (assets SimpleFIN can't reach).",
    no_args_is_help=True,
)
app.add_typer(account_app, name="account")


def _open_writable_store() -> DuckDBStore:
    """Open the configured DuckDBStore in write mode for an `account` command."""
    config = load_config()
    target = db_path(config)
    if not target.exists():
        typer.secho(
            f"No DuckDB store at {target}. Run `goetta-finance init` first.",
            fg=typer.colors.RED,
            err=True,
        )
        raise typer.Exit(code=1)
    store = DuckDBStore(target)
    store.init()
    return store


def _parse_decimal(value: str, *, field: str) -> Decimal:
    try:
        return Decimal(value.strip())
    except (InvalidOperation, ValueError) as exc:
        raise typer.BadParameter(
            f"{field} must be a number, got {value!r}", param_hint=f"--{field}"
        ) from exc


def _parse_as_of(value: str | None) -> datetime:
    """Parse --as-of YYYY-MM-DD into a UTC datetime. Default: now(UTC)."""
    if value is None:
        return datetime.now(tz=UTC)
    try:
        date = datetime.strptime(value, "%Y-%m-%d").replace(tzinfo=UTC)
    except ValueError as exc:
        raise typer.BadParameter(
            f"--as-of must be YYYY-MM-DD, got {value!r}", param_hint="--as-of"
        ) from exc
    if date > datetime.now(tz=UTC):
        raise typer.BadParameter("--as-of cannot be in the future", param_hint="--as-of")
    return date


def _parse_account_type(value: str) -> AccountType:
    try:
        return AccountType(value.lower())
    except ValueError as exc:
        valid = ", ".join(t.value for t in AccountType)
        raise typer.BadParameter(
            f"--type must be one of: {valid} (got {value!r})", param_hint="--type"
        ) from exc


@account_app.command("add")
def account_add(
    name: Annotated[str | None, typer.Option("--name", help="Account display name.")] = None,
    org: Annotated[
        str | None,
        typer.Option("--org", help="Institution or source label (e.g. 'Apple')."),
    ] = None,
    type_: Annotated[
        str | None,
        typer.Option(
            "--type",
            help="Account type: checking, savings, credit, investment, loan, other.",
        ),
    ] = None,
    balance: Annotated[
        str | None,
        typer.Option("--balance", help="Current balance (e.g. 30000 or 30000.50)."),
    ] = None,
    as_of: Annotated[
        str | None,
        typer.Option(
            "--as-of",
            help="Balance observation date (YYYY-MM-DD). Default: today (UTC).",
        ),
    ] = None,
    liability: Annotated[
        bool,
        typer.Option(
            "--liability/--no-liability",
            help="Mark this account as a liability (debt). Subtracts from net worth.",
        ),
    ] = False,
    currency: Annotated[
        str,
        typer.Option(
            "--currency",
            help="ISO 4217 currency code (e.g. USD, EUR, GBP). Default USD.",
        ),
    ] = "USD",
) -> None:
    """Add a manual account. Prompts interactively for any missing values."""
    # Validate provided flag values up-front, before any interactive prompt.
    # Otherwise a bad --type or future --as-of would only surface after the
    # user already answered an unrelated prompt — and under non-interactive
    # invocation (test runners, scripts piping stdin) those prompts read EOF
    # and abort with a confusing "Aborted." message instead of the real
    # validation error.
    if type_ is not None:
        account_type_from_flag: AccountType | None = _parse_account_type(type_)
    else:
        account_type_from_flag = None
    balance_date = _parse_as_of(as_of)
    balance_from_flag: Decimal | None = (
        _parse_decimal(balance, field="balance") if balance is not None else None
    )

    try:
        store = _open_writable_store()
    except GoettaFinanceError as exc:
        typer.secho(f"account add failed: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1) from exc
    try:
        if not name:
            name = typer.prompt("Account name").strip()
            if not name:
                typer.secho("Name cannot be empty.", fg=typer.colors.RED, err=True)
                raise typer.Exit(code=1)
        if org is None:
            org = typer.prompt("Institution / source (optional)", default="").strip() or None
        if account_type_from_flag is None:
            type_prompt = typer.prompt(
                "Type (checking/savings/credit/investment/loan/other)",
                default="other",
            ).strip()
            account_type = _parse_account_type(type_prompt)
        else:
            account_type = account_type_from_flag
        if balance_from_flag is None:
            balance_value = _parse_decimal(typer.prompt("Initial balance").strip(), field="balance")
        else:
            balance_value = balance_from_flag

        account_id = f"{MANUAL_ID_PREFIX}{uuid.uuid4()}"
        account = Account(
            id=account_id,
            org_id=None,
            org_name=org,
            name=name,
            currency=currency.strip().upper(),
            balance=balance_value,
            available_balance=None,
            balance_date=balance_date,
            type=account_type,
            extra={},
            is_manual=True,
            is_liability=liability,
        )
        store.upsert_accounts([account])
        store.record_balance_snapshot(
            BalanceSnapshot(account_id=account_id, balance=balance_value, timestamp=balance_date)
        )
        typer.echo(f"Added {account_id}")
        tags = " [liability]" if liability else ""
        typer.echo(
            f"  {org or '—'} / {name}{tags}: {balance_value:.2f} {account.currency} as of "
            f"{balance_date.astimezone().isoformat(timespec='seconds')}"
        )
    except GoettaFinanceError as exc:
        typer.secho(f"account add failed: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1) from exc
    finally:
        store.close()


@account_app.command("list")
def account_list() -> None:
    """List all accounts (SimpleFIN + manual). Manual accounts are marked."""
    try:
        store = _open_writable_store()
    except GoettaFinanceError as exc:
        typer.secho(f"account list failed: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1) from exc
    try:
        # Include hidden so the user can find what they've hidden in
        # order to unhide it. Tags surface the state.
        accounts = store.get_accounts(include_hidden=True)
        if not accounts:
            typer.echo("No accounts yet.")
            return
        for a in accounts:
            manual_tag = "[manual]" if a.is_manual else "        "
            liability_tag = " [liability]" if a.is_liability else ""
            hidden_tag = " [hidden]" if a.is_hidden else ""
            org = a.org_name or "—"
            typer.echo(
                f"  {manual_tag} {a.id}  {org} / {a.name}{liability_tag}{hidden_tag}: "
                f"{a.balance:.2f} {a.currency}"
            )
    finally:
        store.close()


def _parse_bool(value: str, *, field: str) -> bool:
    """Accept the common true/false spellings; raise BadParameter otherwise."""
    lowered = value.strip().lower()
    if lowered in ("true", "t", "yes", "y", "1"):
        return True
    if lowered in ("false", "f", "no", "n", "0"):
        return False
    raise typer.BadParameter(
        f"{field} must be true/false (or yes/no), got {value!r}", param_hint=field
    )


@account_app.command("set-liability")
def account_set_liability(
    account_id: Annotated[
        str, typer.Argument(help="Account id (any account, manual or SimpleFIN).")
    ],
    value: Annotated[
        str,
        typer.Argument(help="true or false (also accepts yes/no/1/0)."),
    ],
) -> None:
    """Mark an account as a liability (or clear the flag).

    Works on any account id — SimpleFIN-sourced credit cards and manual
    debts both supported. Toggling the flag is retroactive: historical
    balance_snapshots get re-treated under the new flag value in
    net-worth-over-time charts. That's almost always what you want; if
    not, the snapshots aren't editable through this command — flip the
    flag back.
    """
    parsed = _parse_bool(value, field="value")
    try:
        store = _open_writable_store()
    except GoettaFinanceError as exc:
        typer.secho(f"account set-liability failed: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1) from exc
    try:
        store.set_account_liability(account_id, parsed)
        state = "liability" if parsed else "not a liability"
        typer.echo(f"{account_id} is now marked as {state}.")
    except GoettaFinanceError as exc:
        typer.secho(f"account set-liability failed: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1) from exc
    finally:
        store.close()


@account_app.command("set-hidden")
def account_set_hidden(
    account_id: Annotated[
        str, typer.Argument(help="Account id (any account, manual or SimpleFIN).")
    ],
    value: Annotated[
        str,
        typer.Argument(help="true or false (also accepts yes/no/1/0)."),
    ],
) -> None:
    """Hide an account from default views (or unhide it).

    Hidden accounts disappear from ``list_accounts``, the dashboard
    Accounts page, the net-worth chart, transactions queries, and
    spending_by_category. They stay visible in ``goetta-finance
    account list`` (with a ``[hidden]`` tag) so you can find them to
    unhide.

    Use this for stale duplicate accounts that SimpleFIN keeps
    returning, or any account you don't want included in totals. The
    flag survives sync — the upsert's ON CONFLICT SET clause omits
    user-owned columns by design.
    """
    parsed = _parse_bool(value, field="value")
    try:
        store = _open_writable_store()
    except GoettaFinanceError as exc:
        typer.secho(f"account set-hidden failed: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1) from exc
    try:
        store.set_account_hidden(account_id, parsed)
        state = "hidden" if parsed else "visible"
        typer.echo(f"{account_id} is now {state}.")
    except GoettaFinanceError as exc:
        typer.secho(f"account set-hidden failed: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1) from exc
    finally:
        store.close()


@account_app.command("set-balance")
def account_set_balance(
    account_id: Annotated[str, typer.Argument(help="Manual account id (MANUAL-<uuid>).")],
    balance: Annotated[str, typer.Argument(help="New balance.")],
    as_of: Annotated[
        str | None,
        typer.Option(
            "--as-of",
            help="Balance observation date (YYYY-MM-DD). Default: today (UTC).",
        ),
    ] = None,
) -> None:
    """Update the balance on a manual account.

    Writes both ``accounts.balance`` and a new ``balance_snapshots`` row so
    net-worth-over-time reflects the change. Refuses non-manual accounts.
    """
    try:
        store = _open_writable_store()
    except GoettaFinanceError as exc:
        typer.secho(f"account set-balance failed: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1) from exc
    try:
        existing = next((a for a in store.get_accounts() if a.id == account_id), None)
        if existing is None:
            typer.secho(f"account not found: {account_id}", fg=typer.colors.RED, err=True)
            raise typer.Exit(code=1)
        if not existing.is_manual:
            typer.secho(
                f"refusing to update non-manual account: {account_id}",
                fg=typer.colors.RED,
                err=True,
            )
            raise typer.Exit(code=1)
        balance_value = _parse_decimal(balance, field="balance")
        balance_date = _parse_as_of(as_of)
        updated = existing.model_copy(
            update={"balance": balance_value, "balance_date": balance_date}
        )
        store.upsert_accounts([updated])
        store.record_balance_snapshot(
            BalanceSnapshot(account_id=account_id, balance=balance_value, timestamp=balance_date)
        )
        typer.echo(
            f"Updated {account_id}: {balance_value:.2f} {existing.currency} as of "
            f"{balance_date.astimezone().isoformat(timespec='seconds')}"
        )
    except GoettaFinanceError as exc:
        typer.secho(f"account set-balance failed: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1) from exc
    finally:
        store.close()


@account_app.command("remove")
def account_remove(
    account_id: Annotated[str, typer.Argument(help="Manual account id (MANUAL-<uuid>).")],
    force: Annotated[
        bool,
        typer.Option(
            "--force",
            help="Cascade-delete any balance_snapshots rows for this account.",
        ),
    ] = False,
    yes: Annotated[
        bool,
        typer.Option(
            "--yes",
            help="Skip the typed-name confirmation prompt (for scripts).",
        ),
    ] = False,
) -> None:
    """Remove a manual account.

    Two-layer safety:

    1. Refuses any account whose id doesn't start with ``MANUAL-``.
    2. If the account has linked ``balance_snapshots``, requires ``--force``
       AND prompts for the account name to be typed back (unless ``--yes``).
    """
    if not account_id.startswith(MANUAL_ID_PREFIX):
        typer.secho(
            f"refusing to delete non-manual account: {account_id}",
            fg=typer.colors.RED,
            err=True,
        )
        raise typer.Exit(code=1)
    try:
        store = _open_writable_store()
    except GoettaFinanceError as exc:
        typer.secho(f"account remove failed: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1) from exc
    try:
        existing = next((a for a in store.get_accounts() if a.id == account_id), None)
        if existing is None:
            typer.secho(f"account not found: {account_id}", fg=typer.colors.RED, err=True)
            raise typer.Exit(code=1)
        if not existing.is_manual:
            typer.secho(
                f"refusing to delete non-manual account: {account_id}",
                fg=typer.colors.RED,
                err=True,
            )
            raise typer.Exit(code=1)
        snapshot_count_row = store.conn.execute(
            "SELECT COUNT(*) FROM balance_snapshots WHERE account_id = ?",
            [account_id],
        ).fetchone()
        snapshot_count = int(snapshot_count_row[0]) if snapshot_count_row else 0
        if snapshot_count > 0 and not force:
            typer.secho(
                f"account has {snapshot_count} balance snapshot(s). "
                "Pass --force to remove the account and cascade-delete the snapshots.",
                fg=typer.colors.YELLOW,
                err=True,
            )
            raise typer.Exit(code=1)
        if snapshot_count > 0 and not yes:
            typer.echo(
                f"This will delete {existing.name} ({account_id}) "
                f"and {snapshot_count} balance snapshot(s)."
            )
            typed = typer.prompt("Type the account name to confirm").strip()
            if typed != existing.name:
                typer.secho("Name did not match. Aborted.", fg=typer.colors.RED, err=True)
                raise typer.Exit(code=1)
        deleted = store.delete_account(account_id, cascade_snapshots=snapshot_count > 0)
        typer.echo(
            f"Removed {account_id} ({existing.name})"
            + (f" and {deleted} balance snapshot(s)" if deleted else "")
        )
    except GoettaFinanceError as exc:
        typer.secho(f"account remove failed: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1) from exc
    finally:
        store.close()


category_app = typer.Typer(
    help="Manage transaction categories and the rules that map descriptions to them.",
    no_args_is_help=True,
)
app.add_typer(category_app, name="category")

transaction_app = typer.Typer(
    help="Manual per-transaction category overrides.",
    no_args_is_help=True,
)
app.add_typer(transaction_app, name="transaction")

_HEX_COLOR_RE = re.compile(r"^#[0-9a-fA-F]{6}$")


def _parse_match_type(value: str) -> str:
    try:
        return parse_match_type(value)
    except RulePatternError as exc:
        raise typer.BadParameter(str(exc), param_hint=exc.param_hint) from exc


def _validate_rule_pattern(pattern: str, match_type: str) -> None:
    """CLI wrapper over the shared validator (``validators.py``).

    The validator logic — and its ReDoS heuristics + GIL rationale —
    lives in ``validators.validate_rule_pattern`` so the CLI and the
    MCP ``add_category_rule`` tool gate the same write surface
    identically. This wrapper only translates the typer-free
    :class:`RulePatternError` into ``typer.BadParameter``.
    """
    try:
        validate_rule_pattern(pattern, match_type)
    except RulePatternError as exc:
        raise typer.BadParameter(str(exc), param_hint=exc.param_hint) from exc


def _suggest_category(store: DuckDBStore, user_input: str) -> str:
    """Return a ' Did you mean "X"?' or list-command fallback string.

    Used to enrich the friendly error wording when the store raises
    ``category not found``. Uses stdlib ``difflib`` for typo distance.
    """
    names = [c.name for c in store.get_categories()]
    matches = difflib.get_close_matches(user_input, names, n=1, cutoff=0.6)
    if matches:
        return f' Did you mean "{matches[0]}"?'
    return " Run `goetta-finance category list` to see available categories."


def _is_category_not_found(exc: GoettaFinanceError) -> bool:
    return "category not found" in str(exc).lower()


@category_app.command("list")
def category_list() -> None:
    """List every category (defaults + user-added) with transaction + rule counts."""
    try:
        store = _open_writable_store()
    except GoettaFinanceError as exc:
        typer.secho(f"category list failed: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1) from exc
    try:
        counts = store.category_counts()
        rule_rows = store.conn.execute(
            "SELECT c.name, COUNT(r.id) FROM categories c "
            "LEFT JOIN category_rules r ON r.category_id = c.id "
            "GROUP BY c.name"
        ).fetchall()
        rule_counts = {row[0]: int(row[1]) for row in rule_rows}
        spending_flags = {c.name: c.is_spending for c in store.get_categories()}
        if not counts:
            typer.echo("No categories yet.")
            return
        typer.echo(f"{'Category':<30} {'Default':<8} {'Txns':>6} {'Rules':>6}")
        for c in counts:
            default = "yes" if c["is_default"] else "no"
            txns = int(c["transaction_count"])
            rules = rule_counts.get(c["name"], 0)
            tag = "" if spending_flags.get(c["name"], True) else " [non-spending]"
            typer.echo(f"{c['name'] + tag:<30} {default:<8} {txns:>6} {rules:>6}")
    finally:
        store.close()


@category_app.command("add")
def category_add(
    name: Annotated[str, typer.Option("--name", help="Category display name.")],
    color: Annotated[
        str | None,
        typer.Option("--color", help="Optional hex color like #27ae60."),
    ] = None,
    spending: Annotated[
        bool,
        typer.Option(
            "--spending/--no-spending",
            help=(
                "Whether this category counts as spending (default: yes). "
                "Pass --no-spending for categories like inter-account "
                "transfers, employer-side payroll deductions, or any "
                "other category that shouldn't appear in the dashboard's "
                "Spending by category pie or spending_by_category totals."
            ),
        ),
    ] = True,
) -> None:
    """Add a new (non-default) category."""
    if color is not None and not _HEX_COLOR_RE.match(color):
        raise typer.BadParameter(
            f"--color must be #RRGGBB (e.g. #27ae60), got {color!r}", param_hint="--color"
        )
    try:
        store = _open_writable_store()
    except GoettaFinanceError as exc:
        typer.secho(f"category add failed: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1) from exc
    try:
        cat = store.add_category(name, color, is_spending=spending)
        tag = "" if cat.is_spending else " [non-spending]"
        typer.echo(f"Added category {cat.name} (id {cat.id}){tag}.")
    except GoettaFinanceError as exc:
        typer.secho(f"category add failed: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1) from exc
    finally:
        store.close()


@category_app.command("set-spending")
def category_set_spending(
    name: Annotated[str, typer.Argument(help="Existing category name (case-insensitive).")],
    value: Annotated[
        str,
        typer.Argument(help="true or false (also accepts yes/no/1/0)."),
    ],
) -> None:
    """Toggle whether a category counts as spending.

    Categories with is_spending=FALSE are excluded by default from the
    dashboard's Spending by category pie and the spending_by_category
    MCP tool. ``Transfers`` and ``Income`` ship as non-spending
    (migration 0006). Use this command to add additional non-spending
    categories — e.g. if you category-tag employer-side 401(k)
    contributions and don't want them in your spending pie.
    """
    parsed = _parse_bool(value, field="value")
    try:
        store = _open_writable_store()
    except GoettaFinanceError as exc:
        typer.secho(f"category set-spending failed: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1) from exc
    try:
        store.set_category_spending(name, parsed)
        state = (
            "is now counted as spending" if parsed else "is now non-spending (excluded by default)"
        )
        typer.echo(f"{name} {state}.")
    except GoettaFinanceError as exc:
        suffix = _suggest_category(store, name) if _is_category_not_found(exc) else ""
        typer.secho(
            f"category set-spending failed: {exc}.{suffix}",
            fg=typer.colors.RED,
            err=True,
        )
        raise typer.Exit(code=1) from exc
    finally:
        store.close()


@category_app.command("set-rule")
def category_set_rule(
    category_name: Annotated[
        str, typer.Argument(help="Existing category name (case-insensitive).")
    ],
    match: Annotated[
        str,
        typer.Option("--match", help="Match type: 'contains' or 'regex'."),
    ] = "contains",
    pattern: Annotated[
        str,
        typer.Option("--pattern", help="Pattern to match against transaction description."),
    ] = "",
    priority: Annotated[
        int,
        typer.Option(
            "--priority",
            help="Lower number = higher precedence when multiple rules match (default 100).",
        ),
    ] = 100,
) -> None:
    """Add a categorization rule. Patterns are validated against a 1s ReDoS smoke test."""
    match_type = _parse_match_type(match)
    _validate_rule_pattern(pattern, match_type)
    try:
        store = _open_writable_store()
    except GoettaFinanceError as exc:
        typer.secho(f"category set-rule failed: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1) from exc
    try:
        rule_id = store.add_rule(
            category_name, match_type=match_type, pattern=pattern, priority=priority
        )
        typer.echo(
            f"Added rule {rule_id}: {category_name} {match_type} {pattern!r} (priority {priority})."
        )
    except GoettaFinanceError as exc:
        suffix = _suggest_category(store, category_name) if _is_category_not_found(exc) else ""
        typer.secho(f"category set-rule failed: {exc}.{suffix}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1) from exc
    finally:
        store.close()


@category_app.command("remove-rule")
def category_remove_rule(
    rule_id: Annotated[int, typer.Argument(help="Rule id (see `category default-rules`).")],
    force: Annotated[
        bool,
        typer.Option("--force", help="Required to remove a default (is_default=TRUE) rule."),
    ] = False,
    yes: Annotated[
        bool,
        typer.Option("--yes", help="Skip the typed-pattern confirmation prompt (for scripts)."),
    ] = False,
) -> None:
    """Remove a rule. Defaults require ``--force`` and a typed-pattern confirmation."""
    try:
        store = _open_writable_store()
    except GoettaFinanceError as exc:
        typer.secho(f"category remove-rule failed: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1) from exc
    try:
        row = store.conn.execute(
            "SELECT r.is_default, r.pattern, r.match_type, c.name "
            "FROM category_rules r JOIN categories c ON c.id = r.category_id "
            "WHERE r.id = ?",
            [rule_id],
        ).fetchone()
        if row is None:
            typer.secho(f"rule not found: {rule_id}", fg=typer.colors.RED, err=True)
            raise typer.Exit(code=1)
        is_default, pattern, match_type, cat_name = (
            bool(row[0]),
            str(row[1]),
            str(row[2]),
            str(row[3]),
        )
        if is_default and not force:
            typer.secho(
                f"refusing to remove default rule {rule_id} "
                f"({cat_name} {match_type} {pattern!r}) without --force.",
                fg=typer.colors.YELLOW,
                err=True,
            )
            raise typer.Exit(code=1)
        if is_default and not yes:
            typer.echo(
                f"This will remove default rule {rule_id}: {cat_name} {match_type} {pattern!r}."
            )
            typed = typer.prompt("Type the pattern to confirm").strip()
            if typed != pattern:
                typer.secho("Pattern did not match. Aborted.", fg=typer.colors.RED, err=True)
                raise typer.Exit(code=1)
        store.remove_rule(rule_id, force=force)
        typer.echo(f"Removed rule {rule_id} ({cat_name} {match_type} {pattern!r}).")
    except GoettaFinanceError as exc:
        typer.secho(f"category remove-rule failed: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1) from exc
    finally:
        store.close()


@category_app.command("default-rules")
def category_default_rules() -> None:
    """List the seeded default rules (is_default = TRUE), grouped by category."""
    try:
        store = _open_writable_store()
    except GoettaFinanceError as exc:
        typer.secho(f"category default-rules failed: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1) from exc
    try:
        rows = store.conn.execute(
            "SELECT c.name, r.id, r.match_type, r.pattern, r.priority "
            "FROM category_rules r JOIN categories c ON c.id = r.category_id "
            "WHERE r.is_default = TRUE "
            "ORDER BY c.name, r.priority, r.pattern"
        ).fetchall()
        if not rows:
            typer.echo("No default rules found (was the 0004 migration applied?).")
            return
        current_cat: str | None = None
        for cat_name, rid, mtype, pattern, priority in rows:
            if cat_name != current_cat:
                if current_cat is not None:
                    typer.echo("")
                typer.secho(f"{cat_name} (priority {priority}, {mtype}):", bold=True)
                current_cat = cat_name
            typer.echo(f"  [rule {rid}]  {pattern}")
    finally:
        store.close()


@transaction_app.command("categorize")
def transaction_categorize(
    transaction_id: Annotated[str, typer.Argument(help="Transaction id (from get_transactions).")],
    category_name: Annotated[str, typer.Argument(help="Category name (case-insensitive).")],
) -> None:
    """Set a manual category override for one transaction. Beats any rule match."""
    try:
        store = _open_writable_store()
    except GoettaFinanceError as exc:
        typer.secho(f"transaction categorize failed: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1) from exc
    try:
        store.set_transaction_override(transaction_id, category_name)
        typer.echo(f"Categorized {transaction_id} as {category_name}.")
    except GoettaFinanceError as exc:
        suffix = _suggest_category(store, category_name) if _is_category_not_found(exc) else ""
        typer.secho(
            f"transaction categorize failed: {exc}.{suffix}",
            fg=typer.colors.RED,
            err=True,
        )
        raise typer.Exit(code=1) from exc
    finally:
        store.close()


@transaction_app.command("uncategorize")
def transaction_uncategorize(
    transaction_id: Annotated[str, typer.Argument(help="Transaction id to clear the override on.")],
) -> None:
    """Clear a manual category override. Idempotent — no-op if none was set."""
    try:
        store = _open_writable_store()
    except GoettaFinanceError as exc:
        typer.secho(f"transaction uncategorize failed: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1) from exc
    try:
        store.clear_transaction_override(transaction_id)
        typer.echo(f"Cleared override for {transaction_id}.")
    except GoettaFinanceError as exc:
        typer.secho(f"transaction uncategorize failed: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1) from exc
    finally:
        store.close()


if __name__ == "__main__":  # pragma: no cover
    sys.exit(app())
