from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from goetta_finance.config import (
    Config,
    config_path,
    db_path,
    home_dir,
    load_config,
    save_config,
)
from goetta_finance.errors import ConfigError


def test_home_dir_uses_goetta_finance_home_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GOETTA_FINANCE_HOME", str(tmp_path))
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)
    assert home_dir() == tmp_path


def test_home_dir_falls_back_to_xdg(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GOETTA_FINANCE_HOME", raising=False)
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    assert home_dir() == tmp_path / "goetta-finance"


def test_home_dir_defaults_when_no_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("GOETTA_FINANCE_HOME", raising=False)
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)
    assert home_dir() == Path.home() / ".local" / "share" / "goetta-finance"


def test_load_config_when_missing_returns_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GOETTA_FINANCE_HOME", str(tmp_path))
    config = load_config()
    assert config.access_url is None
    assert config.backend == "duckdb"


def test_save_then_load_round_trip(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GOETTA_FINANCE_HOME", str(tmp_path))
    saved = Config(access_url="https://user:pass@example.com/sfin", backend="duckdb")
    written_path = save_config(saved)
    assert written_path == config_path()
    loaded = load_config()
    assert loaded == saved


def test_save_creates_parent_dirs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    nested = tmp_path / "does" / "not" / "exist"
    monkeypatch.setenv("GOETTA_FINANCE_HOME", str(nested))
    save_config(Config(access_url="x"))
    assert (nested / "config.json").is_file()


@pytest.mark.skipif(sys.platform.startswith("win"), reason="POSIX file modes only")
def test_save_sets_owner_only_permissions(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GOETTA_FINANCE_HOME", str(tmp_path))
    save_config(Config(access_url="x"))
    mode = os.stat(config_path()).st_mode & 0o777
    assert mode == 0o600


def test_load_rejects_malformed_json(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GOETTA_FINANCE_HOME", str(tmp_path))
    config_path().parent.mkdir(parents=True, exist_ok=True)
    config_path().write_text("not json", encoding="utf-8")
    with pytest.raises(ConfigError):
        load_config()


def test_db_path_uses_configured_filename(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GOETTA_FINANCE_HOME", str(tmp_path))
    cfg = Config(db_filename="custom.duckdb")
    assert db_path(cfg) == tmp_path / "custom.duckdb"
    assert db_path() == tmp_path / "data.duckdb"
