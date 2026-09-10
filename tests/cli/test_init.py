"""Preparation writes an isolated draft without activating a desktop service."""

from __future__ import annotations

import os
import plistlib
import pwd
import subprocess
from pathlib import Path
from unittest.mock import Mock

import pytest
import typer
from click import unstyle
from octomate_cli import init as init_cli
from octomate_cli import service as service_cli
from octomate_cli.mcp import McpPreset
from octomate_cli.service import PlistService, Release, service_typer
from octomate_cli.wizard import base as wizard_base
from prompt_toolkit.application import create_app_session
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput
from pydantic import TypeAdapter
from rich.console import Console
from typer.testing import CliRunner

runner = CliRunner()


@pytest.fixture
def commands(monkeypatch: pytest.MonkeyPatch) -> Mock:
    run = Mock(return_value=subprocess.CompletedProcess([], 0, ""))
    monkeypatch.setattr(init_cli.subprocess, "run", run)
    monkeypatch.setattr(
        init_cli.subprocess,
        "check_output",
        Mock(side_effect=AssertionError("unexpected Git read")),
    )
    monkeypatch.setattr(init_cli.sys, "platform", "darwin")
    monkeypatch.setattr(init_cli.shutil, "which", lambda name: f"/tools/{name}")
    monkeypatch.setattr(
        service_cli,
        "latest_server_release",
        Mock(
            return_value=Release(
                tag_name="octomate-v1.2.3", draft=False, prerelease=False
            )
        ),
    )
    monkeypatch.setattr(
        init_cli,
        "console",
        Console(stderr=True, width=240, highlight=False, markup=False),
    )
    return run


def test_abort_leaves_installation_directory_absent(
    tmp_path: Path, commands: Mock, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(wizard_base, "select_many", Mock(return_value=[]))
    root = tmp_path / "service"
    result = runner.invoke(
        service_typer,
        [
            "init",
            "--prepare",
            "--root",
            str(root),
            "--port",
            "8123",
            "--agent",
            "claude",
            "--channel",
            "none",
        ],
        input="n\n",
    )
    assert result.exit_code == 1
    assert "Aborted" in result.output
    assert not root.exists()
    commands.assert_not_called()


def test_agent_and_channel_selection_are_separate_steps(
    tmp_path: Path, commands: Mock, monkeypatch: pytest.MonkeyPatch
) -> None:
    selections = Mock(side_effect=[["claude", "codex"], [], []])
    monkeypatch.setattr(wizard_base, "select_many", selections)
    result = runner.invoke(
        service_typer,
        ["init", "--prepare", "--root", str(tmp_path / "service")],
        input="8124\ny\n",
    )
    assert result.exit_code == 0, result.output
    headings = [
        "1/7 · Installation",
        "2/7 · Network",
        "3/7 · Agents Tentacles",
        "4/7 · Channels Tentacles",
        "5/7 · MCP Tentacles",
        "6/7 · Review",
        "7/7 · Prepare and validate",
    ]
    positions = [result.output.index(heading) for heading in headings]
    assert positions == sorted(positions)
    assert "Claude Code, Codex" in result.output
    assert "highest stable" in result.output
    preparation = commands.call_args_list[2].args[0]
    assert preparation[4:] == [
        "--port",
        "8124",
        "--agent",
        "claude",
        "--agent",
        "codex",
    ]


def test_unknown_agent_is_refused_before_preparation(
    tmp_path: Path, commands: Mock
) -> None:
    result = runner.invoke(
        service_typer,
        [
            "init",
            "--prepare",
            "--root",
            str(tmp_path / "service"),
            "--port",
            "8123",
            "--agent",
            "unknown",
            "--channel",
            "none",
            "--yes",
        ],
    )
    assert result.exit_code == 2
    assert "supported --agent" in unstyle(result.output)
    assert not (tmp_path / "service").exists()
    commands.assert_not_called()


@pytest.mark.parametrize("missing", ["root", "port", "agent", "console"])
def test_yes_requires_explicit_settings(
    tmp_path: Path, commands: Mock, missing: str
) -> None:
    root = tmp_path / "service"
    arguments = ["init", "--prepare", "--yes"]
    if missing != "root":
        arguments += ["--root", str(root)]
    if missing != "port":
        arguments += ["--port", "8123"]
    if missing != "agent":
        arguments += ["--agent", "claude"]
    if missing != "console":
        arguments += ["--channel", "none"]
    result = runner.invoke(service_typer, arguments)
    assert result.exit_code == 2
    assert "--yes requires" in unstyle(result.output)
    assert not root.exists()
    commands.assert_not_called()


@pytest.mark.parametrize("symlink", [True, False])
def test_prepare_refuses_occupied_or_symlink_roots(
    tmp_path: Path, commands: Mock, symlink: bool
) -> None:
    root = tmp_path / "service"
    occupied = tmp_path / "existing" if symlink else root
    occupied.mkdir()
    existing = occupied / ".env"
    existing.write_text("existing credentials")
    if symlink:
        root.symlink_to(occupied, target_is_directory=True)
    result = runner.invoke(
        service_typer,
        [
            "init",
            "--prepare",
            "--root",
            str(root),
            "--yes",
            "--port",
            "8123",
            "--agent",
            "claude",
            "--channel",
            "none",
        ],
    )
    assert result.exit_code == 2
    assert existing.read_text() == "existing credentials"
    assert list(occupied.iterdir()) == [existing]
    commands.assert_not_called()


def test_prepared_draft_keeps_desktop_identity_without_token_credentials(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, commands: Mock
) -> None:
    root = tmp_path / "service"
    claude_home = tmp_path / "desktop-claude"
    claude_home.mkdir()
    settings = claude_home / "settings.json"
    settings.write_text('{"enabledPlugins":{"example":true}}')
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(claude_home))
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "do-not-copy-token")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "do-not-copy-key")
    monkeypatch.setenv("OCTOMATE__AUTH__API_KEY_SALT", "do-not-copy-salt")
    result = runner.invoke(
        service_typer,
        [
            "init",
            "--prepare",
            "--root",
            str(root),
            "--yes",
            "--port",
            "8123",
            "--agent",
            "claude",
            "--channel",
            "trunkline",
        ],
    )
    assert result.exit_code == 0, result.output
    draft = root / "control/io.octomate.server.plist"
    payload = plistlib.loads(draft.read_bytes())
    service = PlistService.model_validate(payload)
    account = pwd.getpwuid(os.getuid())
    assert "UserName" not in payload
    assert service.session_type == "Aqua"
    assert service.domain == f"gui/{os.getuid()}"
    assert service.environment == {
        "HOME": account.pw_dir,
        "USER": account.pw_name,
        "LOGNAME": account.pw_name,
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "OCTOMATE_HOME": str(root / "config"),
        "OCTOMATE_DB_URL": f"sqlite+aiosqlite:///{root / 'octomate.db'}",
        "CLAUDE_CONFIG_DIR": str(claude_home),
        **(
            {"CODEX_HOME": str(Path(os.environ["CODEX_HOME"]).expanduser().resolve())}
            if "CODEX_HOME" in os.environ
            else {}
        ),
        **{
            key: os.environ[key]
            for key in (
                "HTTPS_PROXY",
                "HTTP_PROXY",
                "ALL_PROXY",
                "NO_PROXY",
                "SSL_CERT_FILE",
                "SSL_CERT_DIR",
                "REQUESTS_CA_BUNDLE",
            )
            if key in os.environ
        },
    }
    assert service.arguments == [
        str(root / "app/.venv/bin/octomate"),
        "service",
        "serve",
    ]
    assert service.directory == root
    assert service.stdout == root / "logs/stdout.log"
    assert service.stderr == root / "logs/stderr.log"
    assert draft.stat().st_mode & 0o777 == 0o600
    assert root.stat().st_mode & 0o777 == 0o700
    assert settings.read_text() == '{"enabledPlugins":{"example":true}}'
    assert not (root / "octomate.db").exists()
    assert (
        "Database and account creation; service installation and startup."
        in result.output
    )
    calls = [call.args[0] for call in commands.call_args_list]
    assert calls == [
        [
            "/tools/git",
            "clone",
            "--depth",
            "1",
            "--branch",
            "octomate-v1.2.3",
            "https://github.com/kalynnka/octomate.git",
            str(root / "app"),
        ],
        ["/tools/uv", "sync", "--locked", "--no-dev", "--project", str(root / "app")],
        [
            str(root / "app/.venv/bin/python"),
            "-m",
            "octomate_cli.deployment",
            "prepare",
            "--port",
            "8123",
            "--agent",
            "claude",
            "--channel",
            "trunkline",
        ],
        [str(root / "app/.venv/bin/python"), "-m", "octomate_cli.deployment", "check"],
    ]
    for call in commands.call_args_list:
        assert "CLAUDE_CODE_OAUTH_TOKEN" not in call.kwargs["env"]
        assert "ANTHROPIC_API_KEY" not in call.kwargs["env"]
        assert "OCTOMATE__AUTH__API_KEY_SALT" not in call.kwargs["env"]


def test_repeat_only_validates_and_preserves_credentials_and_draft(
    tmp_path: Path, commands: Mock
) -> None:
    root = tmp_path / "service"
    first = runner.invoke(
        service_typer,
        [
            "init",
            "--prepare",
            "--root",
            str(root),
            "--yes",
            "--port",
            "8123",
            "--agent",
            "claude",
            "--channel",
            "none",
        ],
    )
    assert first.exit_code == 0, first.output
    secret = root / ".env"
    secret.write_text("OCTOMATE__AUTH__API_KEY_SALT=preserve-this-secret")
    draft = root / "control/io.octomate.server.plist"
    before = draft.read_bytes()
    commands.reset_mock()
    result = runner.invoke(
        service_typer, ["init", "--prepare", "--root", str(root), "--yes"]
    )
    assert result.exit_code == 0, result.output
    assert secret.read_text() == "OCTOMATE__AUTH__API_KEY_SALT=preserve-this-secret"
    assert draft.read_bytes() == before
    assert "files and credentials are unchanged" in result.output
    assert [call.args[0] for call in commands.call_args_list] == [
        [str(root / "app/.venv/bin/python"), "-m", "octomate_cli.deployment", "check"]
    ]


def test_failed_preparation_reports_that_service_was_not_activated(
    tmp_path: Path, commands: Mock
) -> None:
    commands.side_effect = subprocess.CalledProcessError(1, ["git", "clone"])
    root = tmp_path / "service"
    result = runner.invoke(
        service_typer,
        [
            "init",
            "--prepare",
            "--root",
            str(root),
            "--yes",
            "--port",
            "8123",
            "--agent",
            "claude",
            "--channel",
            "none",
        ],
    )
    assert result.exit_code == 1
    assert "No service was activated" in result.output
    assert "incomplete preparation files" in result.output
    assert not (root / "control/io.octomate.server.plist").exists()
    assert not (root / "octomate.db").exists()
    assert commands.call_count == 1


def test_working_tree_snapshot_includes_edits_and_untracked_files_but_not_ignored(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, commands: Mock
) -> None:
    source = tmp_path / "source"
    checkout = tmp_path / "checkout"
    source.mkdir()
    checkout.mkdir()
    (source / "tracked.py").write_text("uncommitted edit")
    (source / "new.py").write_text("new untracked source")
    (source / ".env").write_text("ignored secrets")
    (checkout / "tracked.py").write_text("committed version")
    (checkout / "deleted.py").write_text("removed in the working tree")
    git_read = Mock(side_effect=[b"tracked.py\0new.py\0", b"deleted.py\0"])
    monkeypatch.setattr(init_cli.subprocess, "check_output", git_read)
    init_cli.copy_working_tree(source, checkout, "/tools/git")
    assert (checkout / "tracked.py").read_text() == "uncommitted edit"
    assert (checkout / "new.py").read_text() == "new untracked source"
    assert not (checkout / ".env").exists()
    assert not (checkout / "deleted.py").exists()
    assert "--exclude-standard" in git_read.call_args_list[0].args[0]
    assert "--others" in git_read.call_args_list[0].args[0]
    commands.assert_not_called()


def test_working_tree_snapshot_refuses_symlinks_to_desktop_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, commands: Mock
) -> None:
    source = tmp_path / "source"
    checkout = tmp_path / "checkout"
    source.mkdir()
    checkout.mkdir()
    secret = tmp_path / "desktop-secret"
    secret.write_text("keep out of the installation")
    (source / "linked").symlink_to(secret)
    monkeypatch.setattr(
        init_cli.subprocess, "check_output", Mock(side_effect=[b"linked\0", b""])
    )
    with pytest.raises(ValueError, match="inside the checkout"):
        init_cli.copy_working_tree(source, checkout, "/tools/git")
    assert list(checkout.iterdir()) == []
    assert secret.read_text() == "keep out of the installation"
    commands.assert_not_called()


@pytest.mark.parametrize("parent_symlink", [True, False])
def test_working_tree_snapshot_refuses_clone_symlinks_outside_checkout(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    commands: Mock,
    parent_symlink: bool,
) -> None:
    source = tmp_path / "source"
    checkout = tmp_path / "checkout"
    desktop = tmp_path / "desktop"
    source.mkdir()
    checkout.mkdir()
    desktop.mkdir()
    secret = desktop / "file.py"
    secret.write_text("desktop content")
    name = "nested/file.py" if parent_symlink else "file.py"
    origin = source / name
    origin.parent.mkdir(exist_ok=True)
    origin.write_text("working tree replacement")
    if parent_symlink:
        (checkout / "nested").symlink_to(desktop, target_is_directory=True)
    else:
        (checkout / "file.py").symlink_to(secret)
    monkeypatch.setattr(
        init_cli.subprocess,
        "check_output",
        Mock(side_effect=[name.encode() + b"\0", b""]),
    )
    with pytest.raises(ValueError, match="inside the checkout"):
        init_cli.copy_working_tree(source, checkout, "/tools/git")
    assert secret.read_text() == "desktop content"
    commands.assert_not_called()


def test_checkbox_keyboard_selects_multiple_agents() -> None:

    with (
        create_pipe_input() as keys,
        create_app_session(input=keys, output=DummyOutput()),
    ):
        keys.send_text("\x1b[B \x1b[B \r")
        assert init_cli.agents_step(None, console=init_cli.console) == [
            "claude",
            "codex",
            "deepseek",
        ]


def test_checkbox_rejects_empty_agents_and_allows_no_channels() -> None:

    with (
        create_pipe_input() as keys,
        create_app_session(input=keys, output=DummyOutput()),
    ):
        keys.send_text(" \r \r")
        assert init_cli.agents_step(None, console=init_cli.console) == ["claude"]
        keys.send_text(" \r")
        assert init_cli.channels_step(None, console=init_cli.console) == []


def test_checkbox_cancellation_aborts() -> None:

    with (
        create_pipe_input() as keys,
        create_app_session(input=keys, output=DummyOutput()),
    ):
        keys.send_text("\x03")
        with pytest.raises(typer.Abort):
            init_cli.agents_step(None, console=init_cli.console)


def test_channel_templates_do_not_read_credentials(
    tmp_path: Path, commands: Mock, monkeypatch: pytest.MonkeyPatch
) -> None:

    credentials = {
        "SLACK__APP_ID": "A123",
        "SLACK__BOT_TOKEN": "test-slack-bot-secret",
        "SLACK__APP_TOKEN": "test-slack-app-secret",
        "LARK__APP_ID": "cli_123",
        "LARK__APP_SECRET": "test-lark-secret",
        "DISCORD__BOT_TOKEN": "test-discord-secret",
    }
    for key, value in credentials.items():
        monkeypatch.setenv(f"OCTOMATE__CHANNELS__{key}", value)
    root = tmp_path / "service"
    result = runner.invoke(
        service_typer,
        [
            "init",
            "--prepare",
            "--root",
            str(root),
            "--port",
            "8123",
            "--agent",
            "claude",
            "--yes",
            "--channel",
            "slack",
            "--channel",
            "lark",
            "--channel",
            "discord",
            "--channel",
            "trunkline",
        ],
    )
    assert result.exit_code == 0, result.output
    preparation = commands.call_args_list[2]
    assert preparation.args[0][8:] == [
        "--channel",
        "slack",
        "--channel",
        "lark",
        "--channel",
        "discord",
        "--channel",
        "trunkline",
    ]
    assert preparation.kwargs["input"] is None
    assert "CONFIGURATION.md" in result.output
    for value in credentials.values():
        assert value not in result.output
        assert value not in (root / "control/io.octomate.server.plist").read_text()
        assert value not in (root / "logs/prepare.log").read_text()
        for call in commands.call_args_list:
            assert value not in str(call.args)
            assert value not in str(call.kwargs.get("env"))


@pytest.mark.parametrize("channel", ["napcat", "unknown", "none,slack"])
def test_unsupported_channel_refused_before_preparation(
    tmp_path: Path, commands: Mock, channel: str
) -> None:
    root = tmp_path / "service"
    result = runner.invoke(
        service_typer,
        [
            "init",
            "--prepare",
            "--root",
            str(root),
            "--port",
            "8123",
            "--agent",
            "claude",
            "--yes",
            *(
                argument
                for name in channel.split(",")
                for argument in ("--channel", name)
            ),
        ],
    )
    assert result.exit_code == 2
    assert "Supported --channel" in unstyle(result.output)
    assert not root.exists()
    commands.assert_not_called()


def test_yes_prepares_channel_template_without_credentials(
    tmp_path: Path, commands: Mock, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("OCTOMATE__CHANNELS__DISCORD__BOT_TOKEN", raising=False)
    root = tmp_path / "service"
    result = runner.invoke(
        service_typer,
        [
            "init",
            "--prepare",
            "--root",
            str(root),
            "--port",
            "8123",
            "--agent",
            "claude",
            "--channel",
            "discord",
            "--yes",
        ],
    )
    assert result.exit_code == 0, result.output
    assert "Bot token" not in result.output
    assert "CONFIGURATION.md" in result.output
    assert commands.call_args_list[2].args[0][-2:] == ["--channel", "discord"]


@pytest.mark.parametrize("confirm", [True, False])
def test_wizard_collects_mcp_before_review_and_only_prepares_after_confirmation(
    tmp_path: Path, commands: Mock, monkeypatch: pytest.MonkeyPatch, confirm: bool
) -> None:
    root = tmp_path / "service"
    monkeypatch.setattr(wizard_base, "select_many", Mock(return_value=["github"]))
    result = runner.invoke(
        service_typer,
        [
            "init",
            "--prepare",
            "--root",
            str(root),
            "--port",
            "8123",
            "--agent",
            "codex",
            "--channel",
            "none",
        ],
        input="github_work\ntest-client\ny\n" + ("y\n" if confirm else "n\n"),
    )
    assert result.exit_code == (0 if confirm else 1), result.output
    assert "GitHub (github_work, read-only)" in result.output
    assert "workflow" in result.output
    assert "authorize their own accounts later" in result.output
    if not confirm:
        assert not root.exists()
        commands.assert_not_called()
        return
    preparation = commands.call_args_list[2]
    assert preparation.args[0][-1] == "--mcp-presets"
    mcps = TypeAdapter(list[McpPreset]).validate_json(preparation.kwargs["input"])
    assert mcps == [
        McpPreset(
            provider="github",
            name="github_work",
            client_id="test-client",
            read_only=True,
        )
    ]
    assert "test-client" not in str(preparation.args)
    assert "test-client" not in str(preparation.kwargs["env"])
    assert not (root / "octomate.db").exists()


def test_wizard_rejects_empty_mcp_client_id_before_preparation(
    tmp_path: Path, commands: Mock, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "service"
    monkeypatch.setattr(wizard_base, "select_many", Mock(return_value=["github"]))
    result = runner.invoke(
        service_typer,
        [
            "init",
            "--prepare",
            "--root",
            str(root),
            "--port",
            "8123",
            "--agent",
            "codex",
            "--channel",
            "none",
        ],
        input="github_work\n \n",
    )
    assert result.exit_code == 2, result.output
    assert "must not be empty" in unstyle(result.output)
    assert not root.exists()
    commands.assert_not_called()


def test_mcp_checkbox_can_be_skipped_or_cancelled() -> None:
    with (
        create_pipe_input() as keys,
        create_app_session(input=keys, output=DummyOutput()),
    ):
        keys.send_text("\r")
        assert init_cli.mcps_step(interactive=True, console=init_cli.console) == []
        keys.send_text("\x03")
        with pytest.raises(typer.Abort):
            init_cli.mcps_step(interactive=True, console=init_cli.console)
