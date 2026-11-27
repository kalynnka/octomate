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
from websockets.datastructures import Headers
from websockets.exceptions import InvalidStatus
from websockets.http11 import Response


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
        if args[-2] == "add":
            profile = tmp_path / "profiles" / args[3]
            package = profile / "node_modules" / mcp.CONNECTOR_PACKAGE
            package.parent.mkdir(parents=True, exist_ok=True)
            if package.is_symlink():
                package.unlink()
            package.symlink_to(args[-1], target_is_directory=True)
            (profile / "package.json").write_text(
                json.dumps(
                    {
                        "dependencies": {mcp.CONNECTOR_PACKAGE: f"link:{args[-1]}"},
                        "dsh": {"profile": {"bundles": [mcp.CONNECTOR_PACKAGE]}},
                    }
                )
            )
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
    assert len(installation) == 1
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


@pytest.mark.parametrize(
    "error",
    [
        TimeoutError("DSH did not answer"),
        DshCompatibilityError("DSH OAuth authorization failed"),
    ],
)
def test_login_guidance_checks_both_services(
    monkeypatch: pytest.MonkeyPatch, error: Exception
) -> None:
    def unavailable(url: str | None) -> None:
        raise error

    monkeypatch.setattr(oauth, "login", unavailable)
    result = CliRunner().invoke(app, ["deepseek", "mcp", "login"])
    assert result.exit_code == 1
    assert "DSH and the Octomate API and authorization UI" in result.output
    assert "restart" not in result.output
    assert "--dsh-url" in result.output


@pytest.mark.parametrize("status", [401, 403, 404])
def test_login_reports_websocket_rejection_without_credentials(
    monkeypatch: pytest.MonkeyPatch, status: int
) -> None:
    def rejected(url: str | None) -> None:
        raise InvalidStatus(
            Response(status, "rejected", Headers({"Set-Cookie": "secret-cookie"}))
        )

    monkeypatch.setattr(oauth, "login", rejected)
    result = CliRunner().invoke(app, ["deepseek", "mcp", "login"])
    assert result.exit_code == 1
    assert f"HTTP {status}" in result.output
    assert "protocol failed" not in result.output
    assert "secret-cookie" not in result.output
    if status in {401, 403}:
        assert "including its token" in result.output
        assert "--dsh-url" in result.output
    else:
        assert "running DSH Web service" in result.output


def test_install_and_uninstall_use_configured_launcher_without_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, installation: list[list[str]]
) -> None:
    launcher = tmp_path / "DSH source" / "bin.js"
    launcher.parent.mkdir()
    launcher.write_text("#!/usr/bin/env node\n")
    launcher.chmod(0o700)
    settings = CLISettings.model_validate({"deepseek": {"executable": str(launcher)}})
    monkeypatch.setattr(mcp, "cli_settings", lambda: settings)
    resolved: list[str] = []

    def which(command: str) -> str | None:
        resolved.append(command)
        return str(launcher) if command == str(launcher) else None

    monkeypatch.setattr(mcp.shutil, "which", which)
    monkeypatch.setattr(oauth, "login", lambda url: None)
    runner = CliRunner()
    for command in ["install", "uninstall"]:
        result = runner.invoke(
            app, ["deepseek", "mcp", command, "--home", str(tmp_path)]
        )
        assert result.exit_code == 0, result.output
    assert all(command[0] == str(launcher) for command in installation)
    assert resolved == [str(launcher)] * 3


def test_unchanged_install_preserves_files_and_still_logs_in(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, installation: list[list[str]]
) -> None:
    logins: list[str | None] = []
    monkeypatch.setattr(oauth, "login", logins.append)
    patch = tmp_path / "cordis.patch.yml"
    patch.write_text("# user configuration\n[]\n")
    runner = CliRunner()
    args = ["deepseek", "mcp", "install", "--home", str(tmp_path)]
    assert runner.invoke(app, args).exit_code == 0
    files = [path for path in tmp_path.rglob("*") if path.is_file()]
    before = {path: (path.read_bytes(), path.stat().st_mtime_ns) for path in files}
    result = runner.invoke(app, args)
    assert result.exit_code == 0, result.output
    assert len(installation) == 1
    assert logins == [None, None]
    assert before == {
        path: (path.read_bytes(), path.stat().st_mtime_ns) for path in files
    }


@pytest.mark.parametrize("change", ["index.mjs", "package.json", "url"])
def test_reinstall_updates_changed_connector_files(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    installation: list[list[str]],
    change: str,
) -> None:
    monkeypatch.setattr(oauth, "login", lambda url: None)
    runner = CliRunner()
    args = ["deepseek", "mcp", "install", "--home", str(tmp_path)]
    assert runner.invoke(app, args).exit_code == 0
    bundle = tmp_path / "octomate/mcp/web"
    changed_path = bundle / ("cordis.patch.yml" if change == "url" else change)
    expected = changed_path.read_bytes()
    if change == "url":
        args += ["--url", "https://other.test"]
    else:
        changed_path.write_text("old connector content")
    unchanged = {
        path: (path.read_bytes(), path.stat().st_mtime_ns)
        for path in bundle.iterdir()
        if path != changed_path
    }
    result = runner.invoke(app, args)
    assert result.exit_code == 0, result.output
    assert len(installation) == 2
    assert unchanged == {
        path: (path.read_bytes(), path.stat().st_mtime_ns) for path in unchanged
    }
    if change == "url":
        row = yaml.safe_load(changed_path.read_text())[0]["insert"][0]
        assert row["config"]["url"] == "https://other.test/octomate/mcp"
    else:
        assert changed_path.read_bytes() == expected


@pytest.mark.parametrize("missing", ["manifest", "dependency", "bundle", "link"])
def test_unchanged_files_still_install_missing_registration(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    installation: list[list[str]],
    missing: str,
) -> None:
    monkeypatch.setattr(oauth, "login", lambda url: None)
    runner = CliRunner()
    args = ["deepseek", "mcp", "install", "--home", str(tmp_path)]
    assert runner.invoke(app, args).exit_code == 0
    profile = tmp_path / "profiles/web"
    manifest = profile / "package.json"
    if missing == "manifest":
        manifest.unlink()
    elif missing == "link":
        (profile / "node_modules" / mcp.CONNECTOR_PACKAGE).unlink()
    else:
        data = json.loads(manifest.read_text())
        if missing == "dependency":
            data["dependencies"].clear()
        else:
            data["dsh"]["profile"]["bundles"].clear()
        manifest.write_text(json.dumps(data))
    result = runner.invoke(app, args)
    assert result.exit_code == 0, result.output
    assert len(installation) == 2
