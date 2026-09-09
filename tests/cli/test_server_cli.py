from __future__ import annotations

import fcntl
import io
import json
import os
import plistlib
import pwd
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from unittest.mock import patch
from urllib.error import URLError

import pytest
from click import unstyle
from octomate_cli.main import app
from octomate_cli.service import PlistService, latest_server_release
from octomate_protocol.deployment import DatabaseBackup
from typer.testing import CliRunner


@dataclass
class Operations:
    database: Path
    loaded: bool = False
    dirty: bool = False
    current: bool = False
    ahead: bool = False
    fail: str | None = None
    events: list[str] = field(default_factory=list)

    def maintenance(self, action: str, backup: DatabaseBackup | None = None) -> str:
        self.events.append(action)
        if action == self.fail:
            raise subprocess.CalledProcessError(1, ["maintenance", action])
        if action == "backup":
            return DatabaseBackup(database=self.database, backup=None).model_dump_json()
        if action == "migrate":
            assert backup is not None
            assert backup.database == self.database
        return "Verified."

    def launchctl(self, action: str, *arguments: str) -> None:
        self.events.append(action)
        assert arguments
        if action == "bootstrap":
            self.loaded = True
        elif action == "bootout":
            self.loaded = False

    def stop(self) -> None:
        self.launchctl("disable", "gui/test")
        if self.loaded:
            self.launchctl("bootout", "gui/test")

    def git(
        self, arguments: list[str], *, cwd: Path, env: dict[str, str], text: bool
    ) -> str:
        assert cwd.is_dir()
        assert text
        assert "OCTOMATE_DB_URL" in env
        assert "GIT_INDEX_FILE" not in env
        if arguments[1] == "status":
            return " M octomate/app.py" if self.dirty else ""
        if arguments[1] == "merge-base":
            return "older-commit" if self.ahead else "previous-commit"
        assert arguments[1] == "rev-parse"
        if arguments[2] == "FETCH_HEAD^{commit}" and not self.current:
            return "released-commit"
        return "previous-commit"

    def run(
        self,
        arguments: list[str],
        *,
        cwd: Path,
        check: bool,
        env: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        assert cwd.is_dir()
        assert check
        if arguments == ["git", "fetch", "origin", "refs/tags/octomate-v0.0.2"]:
            step = "fetch"
        elif arguments == ["git", "checkout", "--detach", "released-commit"]:
            step = "checkout"
        else:
            assert arguments == [
                "uv",
                "sync",
                "--locked",
                "--no-dev",
                "--project",
                str(cwd),
            ]
            assert env is not None
            assert env["UV_PROJECT_ENVIRONMENT"] == str(cwd / ".venv")
            step = "sync"
        self.events.append(step)
        if step == self.fail:
            raise subprocess.CalledProcessError(1, arguments)
        return subprocess.CompletedProcess(arguments, 0, "", "")


@pytest.fixture
def service(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Operations]:
    directory = tmp_path / "app"
    (directory / ".venv/bin").mkdir(parents=True)
    (directory / ".venv/bin/python").touch()
    database = tmp_path / "octomate.db"
    plist = tmp_path / "server.plist"
    plist.write_bytes(
        plistlib.dumps(
            {
                "Label": "io.octomate.server",
                "WorkingDirectory": str(tmp_path),
                "LimitLoadToSessionType": "Aqua",
                "StandardOutPath": str(tmp_path / "logs/stdout.log"),
                "StandardErrorPath": str(tmp_path / "logs/stderr.log"),
                "ProgramArguments": [
                    str(directory / ".venv/bin/octomate"),
                    "service",
                    "serve",
                ],
                "KeepAlive": True,
                "EnvironmentVariables": {
                    "PATH": "/usr/bin:/bin",
                    "HOME": pwd.getpwuid(os.getuid()).pw_dir,
                    "USER": pwd.getpwuid(os.getuid()).pw_name,
                    "LOGNAME": pwd.getpwuid(os.getuid()).pw_name,
                    "OCTOMATE_HOME": str(tmp_path / "config"),
                    "OCTOMATE_DB_URL": f"sqlite+aiosqlite:///{database}",
                },
            }
        )
    )
    operations = Operations(database=database)
    monkeypatch.setattr(
        "octomate_cli.service.urlopen",
        lambda request, timeout: io.BytesIO(
            b'[{"tag_name":"octomate-v0.0.2","draft":false,"prerelease":false}]'
        ),
    )
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(PlistService, "path", staticmethod(lambda: plist))
    monkeypatch.setattr(PlistService, "require_gui", lambda self: None)
    monkeypatch.setattr(PlistService, "stop", lambda self: operations.stop())
    monkeypatch.setattr(PlistService, "loaded", lambda self: operations.loaded)
    monkeypatch.setattr(
        PlistService,
        "maintenance",
        lambda self, action, backup=None: operations.maintenance(action, backup),
    )
    monkeypatch.setattr(
        PlistService,
        "launchctl",
        lambda self, action, *arguments: operations.launchctl(action, *arguments),
    )
    monkeypatch.setattr(subprocess, "check_output", operations.git)
    monkeypatch.setattr(subprocess, "run", operations.run)
    return plist, operations


def test_start_loads_without_migration(
    service: tuple[Path, Operations],
) -> None:
    _, operations = service
    result = CliRunner().invoke(app, ["service", "start"])
    assert result.exit_code == 0, result.output
    assert operations.events == [
        "ready",
        "disable",
        "stopped",
        "enable",
        "bootstrap",
        "verify",
    ]
    assert operations.loaded


def test_start_loaded_service_only_verifies(service: tuple[Path, Operations]) -> None:
    _, operations = service
    operations.loaded = True
    result = CliRunner().invoke(app, ["service", "start"])
    assert result.exit_code == 0, result.output
    assert operations.events == ["ready", "verify"]


@pytest.mark.parametrize("action", ["start", "restart"])
def test_schema_mismatch_does_not_interrupt_running_service(
    service: tuple[Path, Operations], action: str
) -> None:
    _, operations = service
    operations.loaded = True
    operations.fail = "ready"
    result = CliRunner().invoke(app, ["service", action])
    assert result.exit_code == 1
    assert operations.events == ["ready"]
    assert operations.loaded


def test_restart_preserves_code_and_database(service: tuple[Path, Operations]) -> None:
    _, operations = service
    operations.loaded = True
    result = CliRunner().invoke(app, ["service", "restart"])
    assert result.exit_code == 0, result.output
    assert operations.events == [
        "ready",
        "disable",
        "bootout",
        "stopped",
        "enable",
        "bootstrap",
        "verify",
    ]
    assert operations.loaded


def test_busy_port_prevents_bootstrap(service: tuple[Path, Operations]) -> None:
    _, operations = service
    operations.fail = "stopped"
    result = CliRunner().invoke(app, ["service", "start"])
    assert result.exit_code == 1
    assert operations.events == ["ready", "disable", "stopped", "disable"]
    assert not operations.loaded


def test_upgrade_refuses_its_own_environment_before_mutation(
    service: tuple[Path, Operations], monkeypatch: pytest.MonkeyPatch
) -> None:
    plist, operations = service
    monkeypatch.setattr(sys, "prefix", str(plist.parent / "app/.venv"))
    result = CliRunner().invoke(app, ["service", "upgrade"])
    assert result.exit_code == 2
    assert "standalone CLI" in result.output
    assert operations.events == []
    assert not (plist.parent / "control").exists()


def test_stop_does_not_require_valid_application_config(
    service: tuple[Path, Operations],
) -> None:
    _, operations = service
    operations.loaded = True
    operations.fail = "check"
    result = CliRunner().invoke(app, ["service", "stop"])
    assert result.exit_code == 0, result.output
    assert operations.events == ["disable", "bootout"]
    assert not operations.loaded


@pytest.mark.parametrize(
    "command",
    [
        ["service", "upgrade", "--plist", "unused"],
        ["service", "deploy"],
        ["cli", "upgrade"],
        ["serve"],
        ["invite"],
        ["user"],
    ],
)
def test_removed_commands_are_rejected(command: list[str]) -> None:
    result = CliRunner().invoke(app, command)
    assert result.exit_code == 2
    assert "No such" in unstyle(result.output)


def test_upgrade_fetches_release_before_stop_backup_checkout_sync_migrate_start(
    service: tuple[Path, Operations],
) -> None:
    _, operations = service
    operations.loaded = True
    result = CliRunner().invoke(app, ["service", "upgrade"])
    assert result.exit_code == 0, result.output
    assert operations.events == [
        "check",
        "fetch",
        "disable",
        "bootout",
        "backup",
        "checkout",
        "sync",
        "migrate",
        "enable",
        "bootstrap",
        "verify",
    ]


@pytest.mark.parametrize("failure", ["backup", "checkout", "sync", "migrate", "verify"])
def test_failed_upgrade_stays_disabled(
    service: tuple[Path, Operations], failure: str
) -> None:
    _, operations = service
    operations.loaded = True
    operations.fail = failure
    result = CliRunner().invoke(app, ["service", "upgrade"])
    assert result.exit_code == 1
    assert not operations.loaded
    assert "remains disabled" in result.output
    after_failure = operations.events[operations.events.index(failure) + 1 :]
    assert after_failure == (
        ["disable", "bootout"] if failure == "verify" else ["disable"]
    )


def test_upgrade_refuses_unreviewed_checkout(
    service: tuple[Path, Operations],
) -> None:
    _, operations = service
    operations.loaded = True
    operations.dirty = True
    result = CliRunner().invoke(app, ["service", "upgrade"])
    assert result.exit_code == 1
    assert operations.events == ["check"]
    assert operations.loaded


def test_operation_lock_prevents_overlapping_updates(
    service: tuple[Path, Operations],
) -> None:
    plist, operations = service
    control = plist.parent / "control"
    control.mkdir()
    with (control / "server.lock").open("a") as held:
        fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
        result = CliRunner().invoke(app, ["service", "upgrade"])
    assert result.exit_code == 2
    assert "Another server operation" in result.output
    assert operations.events == []


def test_upgrade_refuses_local_commits_ahead_of_remote(
    service: tuple[Path, Operations],
) -> None:
    _, operations = service
    operations.ahead = True
    result = CliRunner().invoke(app, ["service", "upgrade"])
    assert result.exit_code == 1
    assert "refusing a downgrade" in result.output
    assert "disable" not in operations.events
    assert "sync" not in operations.events
    assert "migrate" not in operations.events


@pytest.mark.parametrize("loaded", [True, False])
def test_current_release_does_not_change_service(
    service: tuple[Path, Operations], loaded: bool
) -> None:
    _, operations = service
    operations.loaded = loaded
    operations.current = True
    result = CliRunner().invoke(app, ["service", "upgrade"])
    assert result.exit_code == 0, result.output
    assert "Already at octomate-v0.0.2" in result.output
    assert operations.events == ["check", "fetch"]
    assert operations.loaded == loaded


def test_failed_release_fetch_keeps_service_running(
    service: tuple[Path, Operations],
) -> None:
    _, operations = service
    operations.loaded = True
    operations.fail = "fetch"
    result = CliRunner().invoke(app, ["service", "upgrade"])
    assert result.exit_code == 1
    assert operations.events == ["check", "fetch"]
    assert operations.loaded


def test_release_lookup_failure_keeps_service_running(
    service: tuple[Path, Operations],
) -> None:
    _, operations = service
    operations.loaded = True
    with patch("octomate_cli.service.urlopen", side_effect=URLError("unavailable")):
        result = CliRunner().invoke(app, ["service", "upgrade"])
    assert result.exit_code == 1
    assert "unavailable" in result.output
    assert operations.events == ["check"]
    assert operations.loaded


@pytest.mark.parametrize(
    ("tag", "draft", "prerelease"),
    [
        ("octomate-v0.0.3", True, False),
        ("octomate-v0.0.3", False, True),
        ("--upload-pack=command", False, False),
        ("octomate-v0.0.3rc1", False, False),
        ("octomate-cli-v0.0.3", False, False),
        ("octomate-protocol-v0.0.3", False, False),
    ],
)
def test_upgrade_skips_other_packages_and_unstable_tags(
    service: tuple[Path, Operations], tag: str, draft: bool, prerelease: bool
) -> None:
    _, operations = service
    payload = json.dumps(
        [
            {"tag_name": tag, "draft": draft, "prerelease": prerelease},
            {"tag_name": "octomate-v0.0.2", "draft": False, "prerelease": False},
        ]
    ).encode()
    with patch("octomate_cli.service.urlopen", return_value=io.BytesIO(payload)):
        result = CliRunner().invoke(app, ["service", "upgrade"])
    assert result.exit_code == 0, result.output
    assert "octomate-v0.0.2" in result.output
    assert "fetch" in operations.events


def test_server_release_lookup_paginates_and_compares_versions() -> None:
    pages = [
        [{"tag_name": "octomate-cli-v0.0.9", "draft": False, "prerelease": False}]
        * 100,
        [
            {"tag_name": f"octomate-v{version}", "draft": False, "prerelease": False}
            for version in ("0.0.9", "0.0.11", "0.0.10")
        ],
    ]
    with patch(
        "octomate_cli.service.urlopen",
        side_effect=[io.BytesIO(json.dumps(page).encode()) for page in pages],
    ) as request:
        assert latest_server_release().tag_name == "octomate-v0.0.11"
    assert [call.args[0].full_url for call in request.call_args_list] == [
        "https://api.github.com/repos/kalynnka/octomate/releases?per_page=100&page=1",
        "https://api.github.com/repos/kalynnka/octomate/releases?per_page=100&page=2",
    ]


@pytest.mark.parametrize(
    "payload",
    [
        b"[]",
        b'[{"tag_name":"octomate-cli-v0.0.2","draft":false,"prerelease":false}]',
        b'[{"tag_name":"octomate-v0.0.2"}]',
    ],
)
def test_missing_or_invalid_server_release_keeps_service_running(
    service: tuple[Path, Operations], payload: bytes
) -> None:
    _, operations = service
    operations.loaded = True
    with patch("octomate_cli.service.urlopen", return_value=io.BytesIO(payload)):
        result = CliRunner().invoke(app, ["service", "upgrade"])
    assert result.exit_code == 1
    assert operations.events == ["check"]
    assert operations.loaded


def test_version_reports_installed_packages() -> None:
    result = CliRunner().invoke(app, ["--version"])
    assert result.exit_code == 0, result.output
    assert {line.split()[0] for line in result.output.splitlines()} == {
        "octomate",
        "octomate-cli",
        "octomate-protocol",
    }


def test_server_group_is_removed() -> None:
    result = CliRunner().invoke(app, ["server"])
    assert result.exit_code == 2
    assert "No such command 'server'" in result.output


@pytest.mark.parametrize(
    "command",
    [
        [],
        ["upgrade"],
        ["service"],
        ["service", "start"],
        ["service", "serve"],
        ["service", "init"],
        ["service", "upgrade"],
        ["service", "invite"],
        ["service", "user"],
    ],
)
def test_client_cli_help_does_not_import_server_package(command: list[str]) -> None:
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys; from typer.testing import CliRunner; "
            "from octomate_cli.main import app; "
            "result = CliRunner().invoke(app, sys.argv[1:]); "
            "assert result.exit_code == 0, result.output; "
            "assert 'octomate' not in sys.modules; print(result.output)",
            *command,
            "--help",
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    assert "Usage:" in result.stdout
