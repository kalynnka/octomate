"""Config location owns tail runtime files; changing its contents leaves tails alone."""

from __future__ import annotations

import asyncio
import fcntl
from pathlib import Path
from typing import Literal
from unittest.mock import AsyncMock

import pytest
from octomate_cli.config import (
    CLISettings,
    cli_settings,
    project_config_path,
    user_config_path,
)
from octomate_cli.main import app
from octomate_cli.streaming import deepseek, files
from octomate_cli.streaming.files import tail_path
from typer.testing import CliRunner

CLIENT_CONFIG = 'url = "https://normal.example"\ntoken = "test-token"\n'
DEBUG_CONFIG = 'url = "http://127.0.0.1:8000"\ntoken = "debug-token"\n'


@pytest.fixture
def client_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("HOME", str(tmp_path))
    user = user_config_path()
    user.parent.mkdir(parents=True)
    user.write_text(CLIENT_CONFIG)
    project = project_config_path()
    project.parent.mkdir()
    return project


def test_runtime_scope_is_config_location_not_values(
    client_config: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    user = tail_path("codex", "session")
    settings = CLISettings()
    client_config.write_text(CLIENT_CONFIG)
    project = tail_path("codex", "session")
    assert CLISettings() == settings
    assert project != user

    client_config.write_text(DEBUG_CONFIG)
    assert tail_path("codex", "session") == project
    client_config.write_text(CLIENT_CONFIG)
    assert tail_path("codex", "session") == project

    workspace = tmp_path / "another-workspace"
    (workspace / ".octomate").mkdir(parents=True)
    monkeypatch.chdir(workspace)
    assert tail_path("codex", "session") == user
    project_config_path().write_text(CLIENT_CONFIG)
    assert tail_path("codex", "session") not in {user, project}


def test_runtime_scope_includes_agent_and_session(client_config: Path) -> None:
    assert (
        len(
            {
                tail_path("claude", "session"),
                tail_path("codex", "session"),
                tail_path("deepseek", "session"),
                tail_path("codex", "another-session"),
            }
        )
        == 4
    )


@pytest.mark.parametrize("agent", ["claude", "codex", "deepseek"])
def test_workspace_tail_does_not_share_user_lock_or_spool(
    agent: Literal["claude", "codex", "deepseek"],
    client_config: Path,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    module = deepseek if agent == "deepseek" else files
    streamed = AsyncMock()
    monkeypatch.setattr(module, "run_tail", streamed)
    monkeypatch.setattr(module, "LOCK_GRACE", 0.0)
    monkeypatch.setattr(deepseek, "session_origin", lambda *_: None)
    user = tail_path(agent, "session")
    user_spool = user.with_suffix(".paths")
    user_spool.write_text("user-child.jsonl\n")
    client_config.write_text(CLIENT_CONFIG)
    project = tail_path(agent, "session")
    command = [
        agent,
        "tail",
        "--session",
        "session",
        "--path",
        str(tmp_path / "t.jsonl"),
    ]
    child = tmp_path / "workspace-child.jsonl"
    if agent == "codex":
        command += ["--agent-path", str(child)]
    runner = CliRunner()

    with user.with_suffix(".lock").open("w") as user_lock:
        fcntl.flock(user_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        result = runner.invoke(app, command)
        assert result.exit_code == 0, result.output
        streamed.assert_awaited_once()
        assert user_spool.read_text() == "user-child.jsonl\n"
        if agent == "codex":
            assert project.with_suffix(".paths").read_text() == f"{child}\n"

        with project.with_suffix(".lock").open("w") as project_lock:
            fcntl.flock(project_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            result = runner.invoke(app, command)
            assert result.exit_code == 0, result.output
            streamed.assert_awaited_once()

        with user.with_suffix(".lock").open() as probe:
            with pytest.raises(BlockingIOError):
                fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)


@pytest.mark.parametrize("agent", ["claude", "codex", "deepseek"])
def test_running_tail_reconnects_with_its_loaded_config_after_toml_switch(
    agent: Literal["claude", "codex", "deepseek"],
    client_config: Path,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    client_config.write_text(CLIENT_CONFIG)
    transcript = tmp_path / "t.jsonl"
    transcript.touch()
    module = deepseek if agent == "deepseek" else files
    connections: list[tuple[str, str, Path | str | None]] = []

    async def connect(
        url: str,
        session_id: str,
        transcript_path: Path,
        cwd: str,
        token: str,
        source: Path | str | None,
    ) -> bool:
        connections.append((url, token, source))
        if len(connections) == 1:
            await asyncio.to_thread(client_config.write_text, DEBUG_CONFIG)
            cli_settings.cache_clear()
            return False
        return True

    monkeypatch.setattr(module, "stream_session", connect)
    monkeypatch.setattr(module.asyncio, "sleep", AsyncMock())
    monkeypatch.setattr(deepseek, "session_origin", lambda *_: None)
    runner = CliRunner()
    result = runner.invoke(
        app, [agent, "tail", "--session", "session", "--path", str(transcript)]
    )
    assert result.exit_code == 0, result.output
    assert len(connections) == 2
    assert connections[0] == connections[1]
    assert connections[0][:2] == (
        f"wss://normal.example/hooks/{agent}/stream",
        "test-token",
    )

    result = runner.invoke(
        app, [agent, "tail", "--session", "fresh-session", "--path", str(transcript)]
    )
    assert result.exit_code == 0, result.output
    assert len(connections) == 3
    assert connections[2][:2] == (
        f"ws://127.0.0.1:8000/hooks/{agent}/stream",
        "debug-token",
    )
