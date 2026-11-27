"""DSH owns connector installation, authorization, and saved OAuth credentials."""

import json
import subprocess
from pathlib import Path

import pytest
import yaml
from octomate_cli.config import CLISettings
from octomate_cli.main import app
from octomate_cli.streaming.deepseek import DshCompatibilityError
from octomate_cli.tentacles import mcp as shared
from octomate_cli.tentacles.deepseek import mcp, oauth
from pydantic import JsonValue
from typer.testing import CliRunner
from uuid_utils.compat import uuid7


@pytest.fixture
def installation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
    monkeypatch.setattr(
        shared,
        "cli_settings",
        lambda: CLISettings.model_construct(url="https://core.test", token="old-key"),
    )
    monkeypatch.setattr(mcp.shutil, "which", lambda name: "dsh")
    commands: list[list[str]] = []

    def run(
        args: list[str], *, env: dict[str, str], check: bool
    ) -> subprocess.CompletedProcess[str]:
        assert env["DSH_HOME"] == str(tmp_path)
        commands.append(args)
        return subprocess.CompletedProcess(args, 0)

    monkeypatch.setattr(mcp.subprocess, "run", run)
    return commands


def test_install_migrates_to_connector_and_starts_native_login(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, installation: list[list[str]]
) -> None:
    runner = CliRunner()
    args = ["deepseek", "mcp", "install", "--home", str(tmp_path)]
    assert runner.invoke(app, [*args, "--api-key"]).exit_code == 0
    patch = tmp_path / "cordis.patch.yml"
    patch.write_text(
        patch.read_text()
        + "# unrelated\n- insert:\n    - id: other\n      name: other\n"
    )
    logins: list[str | None] = []
    monkeypatch.setattr(oauth, "login", logins.append)
    for _ in range(2):
        result = runner.invoke(
            app, [*args, "--dsh-url", "http://localhost:3080/?token=launch"]
        )
        assert result.exit_code == 0, result.output
    assert logins == ["http://localhost:3080/?token=launch"] * 2
    assert len(installation) == 2
    bundle = tmp_path / "octomate/mcp/web"
    assert installation[0] == ["dsh", "plugin", "--profile", "web", "add", str(bundle)]
    assert "old-key" not in patch.read_text()
    assert "# unrelated" in patch.read_text()
    assert yaml.safe_load(patch.read_text())[0]["insert"][0]["id"] == "other"
    manifest = json.loads((bundle / "package.json").read_text())
    assert manifest["name"] == mcp.CONNECTOR_PACKAGE
    assert manifest["dsh"]["bundle"]["patch"] == "./cordis.patch.yml"
    row = yaml.safe_load((bundle / "cordis.patch.yml").read_text())[0]["insert"][0]
    assert row["config"] == {"url": "https://core.test/octomate/mcp"}
    assert (bundle / "index.mjs").is_file()
    shown = runner.invoke(app, ["deepseek", "mcp", "show", "--home", str(tmp_path)])
    assert shown.exit_code == 0
    assert "OAuth" in shown.output
    assert "old-key" not in shown.output
    restored = runner.invoke(app, [*args, "--api-key"])
    assert restored.exit_code == 0, restored.output
    assert installation[-1][-2:] == ["remove", mcp.CONNECTOR_PACKAGE]
    assert not bundle.exists()
    assert "old-key" in patch.read_text()


def test_oauth_uninstall_uses_native_package_manager(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, installation: list[list[str]]
) -> None:
    monkeypatch.setattr(oauth, "login", lambda url: None)
    runner = CliRunner()
    assert (
        runner.invoke(
            app, ["deepseek", "mcp", "install", "--home", str(tmp_path)]
        ).exit_code
        == 0
    )
    result = runner.invoke(
        app, ["deepseek", "mcp", "uninstall", "--home", str(tmp_path)]
    )
    assert result.exit_code == 0, result.output
    assert installation[-1][-2:] == ["remove", mcp.CONNECTOR_PACKAGE]
    assert not (tmp_path / "octomate/mcp/web").exists()


def test_install_failure_does_not_start_login(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        shared,
        "cli_settings",
        lambda: CLISettings.model_construct(url="https://core.test"),
    )

    def run(
        args: list[str], *, env: dict[str, str], check: bool
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(args, 7)

    monkeypatch.setattr(mcp.subprocess, "run", run)
    monkeypatch.setattr(mcp.shutil, "which", lambda name: "dsh")
    monkeypatch.setattr(
        oauth, "login", lambda url: pytest.fail("Login after failed installation")
    )
    result = CliRunner().invoke(
        app, ["deepseek", "mcp", "install", "--home", str(tmp_path)]
    )
    assert result.exit_code == 7


@pytest.mark.parametrize("phase", ["authorized", "failed", "cancelled"])
def test_login_follows_dsh_attempt_and_opens_browser_once(
    monkeypatch: pytest.MonkeyPatch, phase: str
) -> None:
    attempt = str(uuid7())
    opened: list[str] = []
    calls: list[tuple[str, dict[str, JsonValue]]] = []
    monkeypatch.setattr(oauth.webbrowser, "open", lambda url: opened.append(url))

    def call(
        self: oauth.DshHistoryClient, method: str, args: dict[str, JsonValue]
    ) -> JsonValue:
        calls.append((method, args))
        return attempt

    monkeypatch.setattr(oauth.DshHistoryClient, "call", call)

    class Socket:
        stream: str = ""
        phases: list[str]

        def __init__(self) -> None:
            self.phases = [
                "absent",
                "ready",
                "waiting-browser",
                "waiting-browser",
                phase,
            ]

        def __enter__(self) -> "Socket":
            return self

        def __exit__(self, *args: None) -> None:
            pass

        def send(self, body: str) -> None:
            request = json.loads(body)
            assert request["endpoint"] == "mcpManager/watch"
            self.stream = request["streamId"]

        def recv(self, *, timeout: float) -> str:
            current = self.phases.pop(0)
            server: dict[str, JsonValue] = {"name": "octomate"}
            if current not in {"ready", "absent"}:
                server["authorization"] = {
                    "id": attempt,
                    "phase": current,
                    "authorizationUrl": "https://core.test/oauth/authorize?state=native",
                }
            return json.dumps(
                {
                    "type": "item",
                    "streamId": self.stream,
                    "value": [] if current == "absent" else [server],
                }
            )

    def connect(
        url: str, *, additional_headers: dict[str, str], open_timeout: int
    ) -> Socket:
        assert url == "ws://localhost:3080/api/remote.mux"
        return Socket()

    monkeypatch.setattr(oauth, "connect", connect)
    if phase == "authorized":
        oauth.login("http://localhost:3080")
    else:
        with pytest.raises(DshCompatibilityError, match=phase):
            oauth.login("http://localhost:3080")
    assert calls == [
        ("connectorAccounts/start", {"key": "mcp:octomate", "operation": "connect"})
    ]
    assert opened == ["https://core.test/oauth/authorize?state=native"]


@pytest.mark.parametrize("profile", ["", ".", "..", "../other", "/tmp/other"])
@pytest.mark.parametrize("command", ["install", "uninstall", "show"])
def test_profile_cannot_escape_connector_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, command: str, profile: str
) -> None:
    monkeypatch.setattr(
        shared,
        "cli_settings",
        lambda: CLISettings.model_construct(url="https://core.test"),
    )
    result = CliRunner().invoke(
        app, ["deepseek", "mcp", command, "--home", str(tmp_path), "--profile", profile]
    )
    assert result.exit_code == 2
    assert "profile name" in result.output
    assert not list(tmp_path.iterdir())


def test_login_reports_unavailable_host(monkeypatch: pytest.MonkeyPatch) -> None:
    def unavailable(url: str | None) -> None:
        raise TimeoutError("DSH did not answer")

    monkeypatch.setattr(oauth, "login", unavailable)
    result = CliRunner().invoke(app, ["deepseek", "mcp", "login"])
    assert result.exit_code == 1
    assert "Start or restart" in result.output
    assert "--dsh-url" in result.output
