"""The operator CLI's Claude-hook install/uninstall merge behavior."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from octomate.tentacles.agent.claude.typer import claude_typer

runner = CliRunner()
URL = "http://127.0.0.1:9999/hooks/claude"
COMMAND_HOOK = {"type": "command", "command": "echo done"}


def settings_with(tmp_path: Path, document: dict[str, Any]) -> Path:
    path = tmp_path / "settings.json"
    path.write_text(json.dumps(document))
    return path


def read(path: Path) -> Any:
    return json.loads(path.read_text())


def hook_types(groups: list[Any]) -> list[str]:
    return [hook["type"] for group in groups for hook in group["hooks"]]


def test_install_preserves_existing_hooks_and_is_idempotent(tmp_path: Path) -> None:
    path = settings_with(
        tmp_path,
        {"model": "opus", "hooks": {"Stop": [{"hooks": [COMMAND_HOOK]}]}},
    )

    for _ in range(2):  # running twice must not duplicate Octomate's handler
        result = runner.invoke(
            claude_typer, ["hooks", "install", "--url", URL, "--settings", str(path)]
        )
        assert result.exit_code == 0

    document = read(path)
    assert document["model"] == "opus"  # unrelated settings untouched
    hooks = document["hooks"]
    # The pre-existing command hook survives beside one Octomate http handler.
    assert hook_types(hooks["Stop"]) == ["command", "http"]
    assert set(hooks) == {"SessionStart", "UserPromptSubmit", "Stop", "SessionEnd"}


def test_install_replaces_a_stale_octomate_url(tmp_path: Path) -> None:
    path = tmp_path / "settings.json"
    for port in (1111, 2222):
        runner.invoke(
            claude_typer,
            ["hooks", "install", "--url", f"http://127.0.0.1:{port}/hooks/claude",
             "--settings", str(path)],
        )
    stop = read(path)["hooks"]["Stop"]
    urls = {hook["url"] for group in stop for hook in group["hooks"]}
    assert urls == {"http://127.0.0.1:2222/hooks/claude"}  # only the fresh url remains


def test_hints_but_does_not_block_when_claude_is_unconfigured(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class Agents:
        claude = None

    class Config:
        agents = Agents()

    monkeypatch.setattr("octomate.config.OctomateConfig", Config)

    result = runner.invoke(
        claude_typer, ["hooks", "install", "--url", URL, "--settings", str(tmp_path / "s.json")]
    )
    assert result.exit_code == 0  # the command still runs
    assert "not configured" in result.output  # but hints that Claude is absent


def test_uninstall_removes_only_octomate_hooks(tmp_path: Path) -> None:
    path = settings_with(tmp_path, {"hooks": {"Stop": [{"hooks": [COMMAND_HOOK]}]}})
    runner.invoke(claude_typer, ["hooks", "install", "--url", URL, "--settings", str(path)])

    result = runner.invoke(claude_typer, ["hooks", "uninstall", "--settings", str(path)])
    assert result.exit_code == 0

    # Only the command hook remains; the events that held just Octomate's are dropped.
    assert read(path)["hooks"] == {"Stop": [{"hooks": [COMMAND_HOOK]}]}
