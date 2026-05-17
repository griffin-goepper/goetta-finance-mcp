from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from goetta_finance.cli import app

runner = CliRunner()


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
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    for cmd in ("init", "sync", "serve", "web", "status"):
        assert cmd in result.output


def test_web_without_db_exits_with_hint(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GOETTA_FINANCE_HOME", str(tmp_path))
    result = runner.invoke(app, ["web", "--port", "0"])
    assert result.exit_code == 1
    assert "init" in result.output.lower()
