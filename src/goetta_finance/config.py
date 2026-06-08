from __future__ import annotations

import contextlib
import json
import logging
import os
import re
import stat
from pathlib import Path

from pydantic import BaseModel, ConfigDict

from goetta_finance.errors import ConfigError

logger = logging.getLogger(__name__)

CONFIG_FILE = "config.json"
DB_FILE = "data.duckdb"
PREFIXES_FILE = "prefixes.txt"
HOME_ENV = "GOETTA_FINANCE_HOME"
XDG_ENV = "XDG_DATA_HOME"

# Universal default prefix-strip patterns for description normalization
# (used by top_uncategorized_patterns). These three are payment-processor
# prefixes, not bank-specific — they appear the same way regardless of
# which bank issued the card:
#   TST*    Toast (restaurant point-of-sale)
#   SQ *    Square
#   AplPay  Apple Pay (with optional SP/DK channel codes)
# Bank-template prefixes (e.g. US Bank's "Web Authorized Pmt") vary per
# institution and belong in the user's prefixes.txt, not the codebase.
# See CUSTOMIZATION.md.
DEFAULT_PREFIX_STRIP_PATTERNS: tuple[str, ...] = (
    r"TST\*\s*",
    r"SQ\s?\*\s*",
    r"AplPay\s+(SP|DK)?\s*",
)

# Written into prefixes.txt on init so users can see the format and
# uncomment the bank-specific examples that match their institution.
_PREFIXES_FILE_TEMPLATE = """\
# goetta-finance description-prefix strip list
#
# One regex per line. Lines starting with # are comments. Patterns are
# matched case-insensitively against the START of each transaction
# description and stripped before grouping, so that e.g.
# "Web Authorized Pmt Spotify" and "Spotify" normalize to the same
# merchant pattern in top_uncategorized_patterns.
#
# The defaults below are payment-processor prefixes (universal).
# Uncomment or add the bank-template prefixes your institution uses.

TST\\*\\s*
SQ\\s?\\*\\s*
AplPay\\s+(SP|DK)?\\s*

# --- common US bank templates (uncomment what applies to you) ---
# Web Authorized Pmt\\s*
# Debit Purchase -visa\\s*
# Electronic Withdrawal\\s*
# Recurring Debit Purchase\\s*
# Mobile Banking Payment\\s*
# Atm Withdrawal\\s*
# Card Purchase\\s*
"""


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


def prefixes_path() -> Path:
    return home_dir() / PREFIXES_FILE


def write_default_prefixes_file(*, overwrite: bool = False) -> Path:
    """Write the default prefixes.txt template. Called by ``init``.

    Never overwrites an existing file unless asked — the file is
    user-owned once it exists (same posture as config.json).
    """
    path = prefixes_path()
    if path.exists() and not overwrite:
        return path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_PREFIXES_FILE_TEMPLATE, encoding="utf-8")
    return path


def load_prefix_strip_patterns() -> list[re.Pattern[str]]:
    """Load the description-prefix strip list for normalization.

    Reads ``$GOETTA_FINANCE_HOME/prefixes.txt`` (one regex per line,
    ``#`` comments). Falls back to the minimal universal default when
    the file is absent. Invalid regex lines are skipped with a warning
    rather than failing the whole load — a typo in one line shouldn't
    break ``top_uncategorized_patterns``.
    """
    path = prefixes_path()
    if not path.exists():
        return [re.compile(p, re.IGNORECASE) for p in DEFAULT_PREFIX_STRIP_PATTERNS]
    patterns: list[re.Pattern[str]] = []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        logger.warning("Could not read %s (%s); using default prefixes", path, exc)
        return [re.compile(p, re.IGNORECASE) for p in DEFAULT_PREFIX_STRIP_PATTERNS]
    for lineno, raw in enumerate(lines, start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        try:
            patterns.append(re.compile(line, re.IGNORECASE))
        except re.error as exc:
            logger.warning("Skipping invalid prefix regex at %s:%d: %s", path, lineno, exc)
    return patterns


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
