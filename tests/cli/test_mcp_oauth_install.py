"""Native OAuth installs keep grant ownership in the harness, not the CLI."""

import json
import subprocess
import tomllib
from pathlib import Path

import pytest
from octomate_cli.config import CLISettings, cli_settings
from octomate_cli.main import app
from octomate_cli.tentacles import mcp
from typer.testing import CliRunner


@pytest.fixture
def login_commands(monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
    commands: list[list[str]] = []

    def run(args: list[str], *, check: bool) -> subprocess.CompletedProcess[str]:
        commands.append(args)
        return subprocess.CompletedProcess(args, 0)

    monkeypatch.setattr(subprocess, "run", run)
    return commands


@pytest.mark.parametrize("runtime", ["claude", "codex"])
@pytest.mark.parametrize("token", [None, "existing-api-key"])
def test_oauth_install_needs_no_key_and_preserves_other_entries(
    runtime: str,
    token: str | None,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    login_commands: list[list[str]],
) -> None:
    monkeypatch.setattr(
        mcp,
        "cli_settings",
        lambda: CLISettings.model_construct(url="http://127.0.0.1:8000", token=token),
    )
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    if runtime == "claude":
        path = tmp_path / ".claude.json"
        path.write_text(
            json.dumps({"mcpServers": {"other": {"url": "https://other.test"}}})
        )
        options = ["--scope", "user", "--file", str(path)]
    else:
        path = tmp_path / ".codex/config.toml"
        path.parent.mkdir()
        path.write_text(
            '# keep this comment\n[mcp_servers.other]\nurl = "https://other.test"\n'
        )
        options = ["--config-file", str(path)]
    runner = CliRunner()
    # Reinstall must remove an old embedded bearer as well as support fresh installs.
    if token is not None:
        installed = runner.invoke(
            app, [runtime, "mcp", "install", "--api-key", *options]
        )
        assert installed.exit_code == 0, installed.output
        assert token in path.read_text()
    for _ in range(2):
        installed = runner.invoke(app, [runtime, "mcp", "install", "--oauth", *options])
        assert installed.exit_code == 0, installed.output
        assert "OAuth" in installed.output
    assert len(login_commands) == 2
    for command in login_commands:
        assert command[0] == runtime
        assert command[-3:] == ["mcp", "login", "octomate"]
    text = path.read_text()
    assert "Authorization" not in text
    assert "existing-api-key" not in text
    if runtime == "claude":
        servers = json.loads(text)["mcpServers"]
        assert servers["other"] == {"url": "https://other.test"}
        assert servers["octomate"] == {
            "type": "http",
            "url": "http://127.0.0.1:8000/octomate/mcp",
            "headers": {"X-Octomate-Client": "claude"},
        }
    else:
        assert "# keep this comment" in text
        servers = tomllib.loads(text)["mcp_servers"]
        assert servers["other"] == {"url": "https://other.test"}
        assert servers["octomate"] == {
            "url": "http://127.0.0.1:8000/octomate/mcp",
            "http_headers": {"X-Octomate-Client": "codex"},
            "scopes": ["mcp"],
            "tools": {"gateway_teleport": {"approval_mode": "prompt"}},
        }
    shown = runner.invoke(app, [runtime, "mcp", "show", *options])
    assert shown.exit_code == 0, shown.output
    assert "Bearer" not in shown.output
    assert "OAuth" in shown.output
    if token is not None:
        installed = runner.invoke(
            app, [runtime, "mcp", "install", "--api-key", *options]
        )
        assert installed.exit_code == 0, installed.output
        assert token in path.read_text()


def test_deepseek_oauth_missing_dsh_preserves_configuration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("shutil.which", lambda name: None)
    path = tmp_path / "cordis.patch.yml"
    path.write_text("[]\n")
    result = CliRunner().invoke(
        app,
        [
            "deepseek",
            "mcp",
            "install",
            "--oauth",
            "--url",
            "https://dev.test",
            "--home",
            str(tmp_path),
        ],
    )
    assert result.exit_code != 0
    assert "dsh is not on PATH" in result.output
    assert path.read_text() == "[]\n"


@pytest.mark.parametrize("runtime", ["codex", "claude"])
@pytest.mark.parametrize("override", [False, True])
@pytest.mark.parametrize("explicit", [False, True])
def test_install_uses_toml_auth_and_invokes_native_login(
    runtime: str,
    override: bool,
    explicit: bool,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    login_commands: list[list[str]],
) -> None:
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    config = tmp_path / ".octomate/cli.toml"
    config.parent.mkdir()
    config.write_text(
        'url = "https://dev.test"\ntoken = "existing-key"\n'
        + ('mcp_auth = "oauth"\n' if explicit else "")
    )
    args = [runtime, "mcp", "install"] + (["--api-key"] if override else [])
    result = CliRunner().invoke(app, args)
    assert result.exit_code == 0, result.output
    assert len(login_commands) == (0 if override else 1)
    if runtime == "codex":
        entry = tomllib.loads((tmp_path / ".codex/config.toml").read_text())[
            "mcp_servers"
        ]["octomate"]
        headers = entry["http_headers"]
    else:
        entry = json.loads((tmp_path / ".claude.json").read_text())["projects"][
            str(tmp_path)
        ]["mcpServers"]["octomate"]
        headers = entry["headers"]
    assert entry["url"] == "https://dev.test/octomate/mcp"
    assert ("Authorization" in headers) == override


@pytest.mark.parametrize("runtime", ["codex", "claude"])
@pytest.mark.parametrize("missing", [False, True])
def test_install_reports_login_failure(
    runtime: str,
    missing: bool,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(Path, "home", lambda: tmp_path)

    def run(args: list[str], *, check: bool) -> subprocess.CompletedProcess[str]:
        if missing:
            raise FileNotFoundError(runtime)
        return subprocess.CompletedProcess(args, 7)

    monkeypatch.setattr(subprocess, "run", run)
    result = CliRunner().invoke(
        app, [runtime, "mcp", "install", "--oauth", "--url", "https://dev.test"]
    )
    assert result.exit_code == (2 if missing else 7)
    if missing:
        assert "login could not start" in result.output


@pytest.mark.parametrize("runtime", ["codex", "claude"])
def test_oauth_rejects_a_file_native_login_cannot_read(
    runtime: str,
    tmp_path: Path,
    login_commands: list[list[str]],
) -> None:
    path = tmp_path / "custom.json"
    result = CliRunner().invoke(
        app,
        [
            runtime,
            "mcp",
            "install",
            "--oauth",
            "--url",
            "https://dev.test",
            "--file" if runtime == "claude" else "--config-file",
            str(path),
        ],
    )
    assert result.exit_code != 0
    assert "native configuration" in result.output
    assert not path.exists()
    assert login_commands == []


def test_environment_can_override_toml_auth(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = tmp_path / ".octomate/cli.toml"
    config.parent.mkdir()
    config.write_text('mcp_auth = "oauth"\n')
    monkeypatch.setenv("OCTOMATE_CLI_MCP_AUTH", "apikey")
    assert cli_settings().mcp_auth == "apikey"
