from __future__ import annotations

from pathlib import Path

import pytest
import typer
from typer.testing import CliRunner

from goetta_finance.cli import app

runner = CliRunner()


def _root_group():  # type: ignore[no-untyped-def]
    # typer.main.get_command(app) returns the app's Click group. We don't
    # assert its concrete class: a TyperGroup isn't reliably `isinstance` of
    # the imported click.Group across click/typer versions (it was False on
    # CI's newer click). Duck-type the .commands / .params attributes instead.
    return typer.main.get_command(app)


def _command_names() -> set[str]:
    """Top-level command names registered on the app (rendering-independent)."""
    return set(_root_group().commands)


def _option_flags(command: str) -> set[str]:
    """Every option flag (primary + secondary) a subcommand exposes.

    Introspected from Click rather than scraped from Rich-rendered ``--help``,
    which varies by terminal width and typer/rich version — scraping it was a
    CI flake (headless runners + newer rich rendered the options panel
    differently than a local wide terminal).
    """
    flags: set[str] = set()
    for param in _root_group().commands[command].params:
        flags.update(param.opts)
        flags.update(param.secondary_opts)
    return flags


def test_status_when_unconfigured_is_friendly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GOETTA_FINANCE_HOME", str(tmp_path))
    result = runner.invoke(app, ["status"])
    assert result.exit_code == 0
    assert "Not configured" in result.output


def test_sync_when_unconfigured_exits_nonzero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GOETTA_FINANCE_HOME", str(tmp_path))
    result = runner.invoke(app, ["sync"])
    assert result.exit_code == 1
    assert "init" in result.output.lower()


def test_help_lists_commands() -> None:
    assert {"init", "sync", "serve", "web", "daemon", "status"} <= _command_names()


def test_web_without_db_exits_with_hint(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GOETTA_FINANCE_HOME", str(tmp_path))
    result = runner.invoke(app, ["web", "--port", "0"])
    assert result.exit_code == 1
    assert "init" in result.output.lower()


def test_daemon_exposes_expected_flags() -> None:
    assert {"--host", "--port", "--sync-at", "--no-schedule", "--no-mcp"} <= _option_flags("daemon")


def test_daemon_without_config_exits_with_hint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GOETTA_FINANCE_HOME", str(tmp_path))
    result = runner.invoke(app, ["daemon"])
    assert result.exit_code == 1
    assert "init" in result.output.lower()
