from __future__ import annotations

import contextlib
import json
import os
import stat
from pathlib import Path

from pydantic import BaseModel, ConfigDict

from goetta_finance.errors import ConfigError

CONFIG_FILE = "config.json"
DB_FILE = "data.duckdb"
HOME_ENV = "GOETTA_FINANCE_HOME"
XDG_ENV = "XDG_DATA_HOME"


class Config(BaseModel):
    """User configuration. The access_url is sensitive — never log it."""

    model_config = ConfigDict(extra="forbid")

    access_url: str | None = None
    backend: str = "duckdb"
    db_filename: str = DB_FILE


def home_dir() -> Path:
    """Resolve the config home directory.

    Order: ``GOETTA_FINANCE_HOME`` → ``$XDG_DATA_HOME/goetta-finance`` →
    ``~/.local/share/goetta-finance``.
    """
    if env := os.environ.get(HOME_ENV):
        return Path(env)
    if xdg := os.environ.get(XDG_ENV):
        return Path(xdg) / "goetta-finance"
    return Path.home() / ".local" / "share" / "goetta-finance"


def config_path() -> Path:
    return home_dir() / CONFIG_FILE


def db_path(config: Config | None = None) -> Path:
    return home_dir() / (config.db_filename if config else DB_FILE)


def load_config() -> Config:
    path = config_path()
    if not path.exists():
        return Config()
    try:
        with path.open("r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError) as exc:
        raise ConfigError(f"Could not read config at {path}: {exc}") from exc
    try:
        return Config.model_validate(data)
    except ValueError as exc:
        raise ConfigError(f"Config at {path} is invalid: {exc}") from exc


def save_config(config: Config) -> Path:
    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with path.open("w", encoding="utf-8") as f:
            json.dump(
                config.model_dump(mode="json", exclude_none=False),
                f,
                indent=2,
                sort_keys=True,
            )
            f.write("\n")
    except OSError as exc:
        raise ConfigError(f"Could not write config to {path}: {exc}") from exc
    # 0o600 on POSIX; Windows ACLs ignore this so we wrap.
    with contextlib.suppress(OSError):
        os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
    return path
