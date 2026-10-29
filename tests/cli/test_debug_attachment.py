"""Debug attachment switches hooks and MCP without creating project hooks."""

import json
import subprocess
import sys
import tomllib
from pathlib import Path
from typing import Literal
from unittest.mock import Mock

import pytest
from octomate_cli.main import app
from typer.testing import CliRunner

from scripts import debug_attachment


@pytest.fixture
def workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "workspace with spaces"
    config = root / ".octomate"
    config.mkdir(parents=True)
    for name in ("debug", "macmini"):
        (config / f"cli.{name}.toml").write_text(
            f'url = "https://{name}.example"\ntoken = "{name}-token"\n'
        )
    (config / "cli.toml").write_text('url = "https://before.example"\n')
    local_cli = root / ".venv" / "bin" / "octomate"
    local_cli.parent.mkdir(parents=True)
    local_cli.touch()
    home = tmp_path / "user"
    home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: home)
    monkeypatch.chdir(root)
    return root


@pytest.mark.parametrize("mode", ["on", "off"])
def test_switch_updates_mcp_and_user_and_existing_project_hooks(
    workspace: Path, monkeypatch: pytest.MonkeyPatch, mode: Literal["on", "off"]
) -> None:
    project = workspace / ".claude" / "settings.json"
    project.parent.mkdir()
    project.write_text(
        json.dumps(
            {
                "model": "keep-model",
                "hooks": {
                    "UserPromptSubmit": [
                        {
                            "hooks": [
                                {"type": "command", "command": "echo keep"},
                                {
                                    "type": "command",
                                    "command": "/old/python /old/launch.py --path /hooks/claude",
                                },
                            ]
                        }
                    ]
                },
            }
        )
    )
    unrelated = project.with_name("settings.local.json")
    unrelated.write_text('{"permissions": {"allow": ["Read"]}}\n')
    global_cli = workspace.parent / "global tools" / "octomate"
    global_cli.parent.mkdir()
    global_cli.touch()
    cli = workspace / ".venv" / "bin" / "octomate" if mode == "on" else global_cli
    commands: list[list[str]] = []

    def run(
        args: list[str],
        *,
        check: bool,
        capture_output: bool = False,
        text: bool = False,
    ) -> subprocess.CompletedProcess[str]:
        commands.append(args)
        if args[0] == "uv":
            return subprocess.CompletedProcess(args, 0, str(global_cli.parent) + "\n")
        assert Path(args[0]) == cli
        with monkeypatch.context() as context:
            context.setattr(sys, "argv", [str(cli)])
            result = CliRunner().invoke(app, args[1:])
        assert result.exit_code == 0, result.output
        return subprocess.CompletedProcess(args, 0, result.output)

    monkeypatch.setattr(debug_attachment.subprocess, "run", run)
    debug_attachment.main(mode)

    profile = "debug" if mode == "on" else "macmini"
    config = workspace / ".octomate"
    assert (config / "cli.toml").read_bytes() == (
        config / f"cli.{profile}.toml"
    ).read_bytes()
    if mode == "off":
        assert commands[0] == ["uv", "tool", "dir", "--bin"]
        assert sum(command[0] == "uv" for command in commands) == 1
    else:
        assert all(command[0] != "uv" for command in commands)
    for path in (Path.home() / ".claude" / "settings.json", project):
        raw = path.read_text()
        assert str(cli) in raw
        assert "/old/" not in raw
    assert str(cli) in (Path.home() / ".codex" / "hooks.json").read_text()
    codex = tomllib.loads((Path.home() / ".codex" / "config.toml").read_text())
    claude = json.loads((Path.home() / ".claude.json").read_text())
    for entry, headers in (
        (codex["mcp_servers"]["octomate"], "http_headers"),
        (claude["mcpServers"]["octomate"], "headers"),
    ):
        assert entry["url"] == f"https://{profile}.example/octomate/mcp"
        assert entry[headers]["Authorization"] == f"Bearer {profile}-token"
    assert "projects" not in claude
    assert not (workspace / ".codex" / "hooks.json").exists()
    assert unrelated.read_text() == '{"permissions": {"allow": ["Read"]}}\n'
    saved = json.loads(project.read_text())
    assert saved["model"] == "keep-model"
    assert saved["hooks"]["UserPromptSubmit"][0]["hooks"] == [
        {"type": "command", "command": "echo keep"}
    ]


def test_failed_global_lookup_keeps_current_profile_and_hooks(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    active = workspace / ".octomate" / "cli.toml"
    before = active.read_bytes()
    run = Mock(side_effect=subprocess.CalledProcessError(1, ["uv", "tool", "dir"]))
    monkeypatch.setattr(debug_attachment.subprocess, "run", run)

    with pytest.raises(subprocess.CalledProcessError):
        debug_attachment.main("off")

    run.assert_called_once()
    assert active.read_bytes() == before
    assert not (Path.home() / ".claude").exists()
    assert not (Path.home() / ".codex").exists()


def test_missing_profile_stops_before_installing_or_switching_hooks(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (workspace / ".octomate" / "cli.macmini.toml").unlink()
    run = Mock()
    monkeypatch.setattr(debug_attachment.subprocess, "run", run)

    with pytest.raises(FileNotFoundError):
        debug_attachment.main("off")

    run.assert_not_called()
