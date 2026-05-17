"""Detect and write the Claude Desktop / Claude Code MCP config so that
goetta-finance appears as an available server."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

from goetta_finance.errors import ConfigError

SERVER_KEY = "goetta-finance"


def claude_desktop_config_path() -> Path | None:
    """Return the platform-specific path to claude_desktop_config.json, or
    None if the platform is unsupported."""
    if sys.platform == "darwin":
        return (
            Path.home()
            / "Library"
            / "Application Support"
            / "Claude"
            / "claude_desktop_config.json"
        )
    if sys.platform.startswith("win"):
        appdata = os.environ.get("APPDATA")
        if not appdata:
            return None
        return Path(appdata) / "Claude" / "claude_desktop_config.json"
    return None


def resolve_command() -> str:
    """Best path to use as the ``command`` in claude_desktop_config.json.

    Claude Desktop spawns the MCP server in its own environment — it does
    not inherit the user's venv ``PATH``, so a bare ``goetta-finance``
    string only works if the executable is on the system-wide ``PATH``.
    Resolution order:

    1. ``Scripts/goetta-finance(.exe)`` next to the current Python
       interpreter (catches venv and pipx installs).
    2. ``sys.argv[0]`` if it exists as a file (catches direct invocation).
    3. The bare name ``goetta-finance`` (fall back — preserves prior
       behaviour for system-wide installs).
    """
    python_dir = Path(sys.executable).parent
    for name in ("goetta-finance.exe", "goetta-finance"):
        candidate = python_dir / name
        if candidate.is_file():
            return str(candidate)
    if sys.argv:
        arg0 = Path(sys.argv[0])
        if arg0.is_file():
            return str(arg0.resolve())
    return "goetta-finance"


def build_server_entry(command: str = "goetta-finance") -> dict[str, Any]:
    return {"command": command, "args": ["serve"]}


def merge_into_config(
    existing: dict[str, Any], entry: dict[str, Any]
) -> tuple[dict[str, Any], bool]:
    """Insert/replace our server entry in a claude_desktop_config.json dict.

    Returns (new_config, changed). Preserves any other configured servers.
    """
    merged = dict(existing)
    servers = dict(merged.get("mcpServers") or {})
    changed = servers.get(SERVER_KEY) != entry
    servers[SERVER_KEY] = entry
    merged["mcpServers"] = servers
    return merged, changed


def write_claude_desktop_config(path: Path, *, command: str = "goetta-finance") -> bool:
    """Merge goetta-finance into the Claude Desktop config at ``path``.

    Returns True if the file was changed (created or modified). Preserves
    existing servers and other top-level keys.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    existing: dict[str, Any] = {}
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

    merged, changed = merge_into_config(existing, build_server_entry(command))
    if not changed:
        return False

    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(merged, f, indent=2, sort_keys=True)
        f.write("\n")
    os.replace(tmp, path)
    return True


def claude_code_executable() -> str | None:
    """Return the path to the Claude Code CLI if it's on PATH."""
    return shutil.which("claude")


def register_with_claude_code(
    command: str, *, name: str = SERVER_KEY, scope: str = "user"
) -> tuple[bool, str]:
    """Register goetta-finance with Claude Code via ``claude mcp add``.

    Returns ``(success, message)``. On failure, ``message`` contains the
    captured stderr/stdout so the caller can surface it. Idempotent only in
    the sense that a duplicate registration surfaces as a clean failure —
    callers wanting to replace should ``claude mcp remove <name>`` first.
    """
    claude = claude_code_executable()
    if claude is None:
        return False, "`claude` CLI not on PATH"

    args = [claude, "mcp", "add", name, "--scope", scope, "--", command, "serve"]
    try:
        result = subprocess.run(args, capture_output=True, text=True, check=False)
    except OSError as exc:
        return False, f"Failed to invoke `claude`: {exc}"

    if result.returncode != 0:
        msg = result.stderr.strip() or result.stdout.strip()
        return False, msg or f"`claude mcp add` exited with code {result.returncode}"
    return True, result.stdout.strip()
