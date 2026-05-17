from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

from goetta_finance.errors import ConfigError
from goetta_finance.mcp_config import (
    SERVER_KEY,
    build_server_entry,
    claude_code_executable,
    claude_desktop_config_path,
    merge_into_config,
    register_with_claude_code,
    resolve_command,
    write_claude_desktop_config,
)


def test_merge_preserves_other_servers() -> None:
    existing = {
        "mcpServers": {"other": {"command": "x", "args": []}},
        "unrelated": True,
    }
    merged, changed = merge_into_config(existing, build_server_entry())
    assert changed is True
    assert merged["mcpServers"]["other"] == {"command": "x", "args": []}
    assert merged["mcpServers"][SERVER_KEY] == build_server_entry()
    assert merged["unrelated"] is True


def test_merge_idempotent_when_entry_matches() -> None:
    entry = build_server_entry()
    existing = {"mcpServers": {SERVER_KEY: entry}}
    _, changed = merge_into_config(existing, entry)
    assert changed is False


def test_merge_replaces_stale_entry() -> None:
    stale = {"command": "old-name", "args": ["serve"]}
    existing = {"mcpServers": {SERVER_KEY: stale}}
    merged, changed = merge_into_config(existing, build_server_entry())
    assert changed is True
    assert merged["mcpServers"][SERVER_KEY] == build_server_entry()


def test_write_creates_file_when_missing(tmp_path: Path) -> None:
    target = tmp_path / "nested" / "claude_desktop_config.json"
    changed = write_claude_desktop_config(target)
    assert changed is True
    data = json.loads(target.read_text(encoding="utf-8"))
    assert data["mcpServers"][SERVER_KEY] == build_server_entry()


def test_write_preserves_existing_servers(tmp_path: Path) -> None:
    target = tmp_path / "claude_desktop_config.json"
    target.write_text(
        json.dumps({"mcpServers": {"other": {"command": "x", "args": []}}}),
        encoding="utf-8",
    )
    write_claude_desktop_config(target)
    data = json.loads(target.read_text(encoding="utf-8"))
    assert "other" in data["mcpServers"]
    assert SERVER_KEY in data["mcpServers"]


def test_write_is_idempotent(tmp_path: Path) -> None:
    target = tmp_path / "claude_desktop_config.json"
    assert write_claude_desktop_config(target) is True
    assert write_claude_desktop_config(target) is False


def test_write_rejects_non_object_file(tmp_path: Path) -> None:
    target = tmp_path / "claude_desktop_config.json"
    target.write_text("[1, 2, 3]", encoding="utf-8")
    with pytest.raises(ConfigError):
        write_claude_desktop_config(target)


def test_write_rejects_malformed_json(tmp_path: Path) -> None:
    target = tmp_path / "claude_desktop_config.json"
    target.write_text("not json", encoding="utf-8")
    with pytest.raises(ConfigError):
        write_claude_desktop_config(target)


@pytest.mark.skipif(not sys.platform.startswith("win"), reason="Windows-only path")
def test_claude_config_path_windows(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("APPDATA", r"C:\Users\test\AppData\Roaming")
    path = claude_desktop_config_path()
    assert path is not None
    assert str(path).endswith(os.path.join("Roaming", "Claude", "claude_desktop_config.json"))


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS-only path")
def test_claude_config_path_macos() -> None:
    path = claude_desktop_config_path()
    assert path is not None
    assert "Library/Application Support/Claude" in str(path)


@pytest.mark.skipif(
    sys.platform == "darwin" or sys.platform.startswith("win"),
    reason="Unsupported platforms only",
)
def test_claude_config_path_unsupported_returns_none() -> None:
    assert claude_desktop_config_path() is None


def test_resolve_command_finds_script_next_to_python(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A venv layout where ``Scripts/goetta-finance(.exe)`` sits beside
    ``Scripts/python.exe`` is the common case (venv, pipx, uvx)."""
    scripts_dir = tmp_path / "Scripts"
    scripts_dir.mkdir()
    fake_python = scripts_dir / "python.exe"
    fake_python.write_text("", encoding="utf-8")
    exe_name = "goetta-finance.exe" if sys.platform.startswith("win") else "goetta-finance"
    fake_cli = scripts_dir / exe_name
    fake_cli.write_text("", encoding="utf-8")

    monkeypatch.setattr(sys, "executable", str(fake_python))
    assert resolve_command() == str(fake_cli)


def test_resolve_command_falls_back_to_bare_name(monkeypatch: pytest.MonkeyPatch) -> None:
    """When no sibling script exists and ``sys.argv[0]`` isn't a real file
    (e.g. running pytest), the bare name is returned — preserves the prior
    behaviour for users who installed system-wide."""
    monkeypatch.setattr(sys, "executable", "/no/such/python")
    monkeypatch.setattr(sys, "argv", ["pytest"])
    assert resolve_command() == "goetta-finance"


def test_write_uses_provided_command(tmp_path: Path) -> None:
    """The init flow passes ``resolve_command()`` so Claude Desktop gets
    a usable path even when the venv isn't on the system PATH."""
    target = tmp_path / "claude_desktop_config.json"
    abs_command = str(tmp_path / "venv" / "Scripts" / "goetta-finance.exe")
    write_claude_desktop_config(target, command=abs_command)
    data = json.loads(target.read_text(encoding="utf-8"))
    assert data["mcpServers"][SERVER_KEY]["command"] == abs_command


def test_claude_code_executable_uses_path(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("shutil.which", lambda cmd: "/usr/bin/claude" if cmd == "claude" else None)
    assert claude_code_executable() == "/usr/bin/claude"


def test_claude_code_executable_returns_none_when_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("shutil.which", lambda cmd: None)
    assert claude_code_executable() is None


def test_register_with_claude_code_returns_false_when_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("shutil.which", lambda cmd: None)
    ok, msg = register_with_claude_code("/path/to/goetta-finance.exe")
    assert ok is False
    assert "claude" in msg.lower()


def test_register_with_claude_code_invokes_subprocess(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import subprocess

    monkeypatch.setattr("shutil.which", lambda cmd: "/usr/bin/claude")
    calls: list[list[str]] = []

    class _Result:
        returncode = 0
        stdout = "Added MCP server goetta-finance"
        stderr = ""

    def fake_run(args: list[str], **kwargs: object) -> _Result:
        calls.append(args)
        return _Result()

    monkeypatch.setattr(subprocess, "run", fake_run)
    ok, msg = register_with_claude_code("/path/goetta-finance.exe")
    assert ok is True
    assert "Added" in msg
    assert calls == [
        [
            "/usr/bin/claude",
            "mcp",
            "add",
            SERVER_KEY,
            "--scope",
            "user",
            "--",
            "/path/goetta-finance.exe",
            "serve",
        ]
    ]


def test_register_with_claude_code_surfaces_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    import subprocess

    monkeypatch.setattr("shutil.which", lambda cmd: "/usr/bin/claude")

    class _Result:
        returncode = 1
        stdout = ""
        stderr = "MCP server `goetta-finance` already exists"

    monkeypatch.setattr(subprocess, "run", lambda *a, **kw: _Result())
    ok, msg = register_with_claude_code("/path/goetta-finance.exe")
    assert ok is False
    assert "already exists" in msg
