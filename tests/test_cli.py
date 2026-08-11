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


# --- backup configure -------------------------------------------------


def test_backup_configure_shows_settings_without_writing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GOETTA_FINANCE_HOME", str(tmp_path))
    result = runner.invoke(app, ["backup", "configure"])
    assert result.exit_code == 0, result.output
    assert "enabled:      True" in result.output
    assert str(tmp_path / "backups") in result.output
    assert not (tmp_path / "config.json").exists()  # read-only run wrote nothing


def test_backup_configure_persists_the_destination(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from goetta_finance.config import load_config

    monkeypatch.setenv("GOETTA_FINANCE_HOME", str(tmp_path))
    cloud = tmp_path / "OneDrive" / "goetta-backups"
    cloud.mkdir(parents=True)
    result = runner.invoke(app, ["backup", "configure", "--dir", str(cloud), "--keep-daily", "30"])
    assert result.exit_code == 0, result.output

    config = load_config()
    assert config.backup.directory == str(cloud.resolve())
    assert config.backup.keep_daily == 30
    assert config.backup.enabled is True


def test_backup_configure_can_disable_scheduled_backups(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from goetta_finance.config import load_config

    monkeypatch.setenv("GOETTA_FINANCE_HOME", str(tmp_path))
    result = runner.invoke(app, ["backup", "configure", "--disable"])
    assert result.exit_code == 0, result.output
    assert load_config().backup.enabled is False
    assert "after each successful sync" not in result.output


def test_backup_configure_rejects_a_negative_retention(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GOETTA_FINANCE_HOME", str(tmp_path))
    result = runner.invoke(app, ["backup", "configure", "--keep-daily", "-1"])
    assert result.exit_code != 0
