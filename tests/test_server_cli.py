from __future__ import annotations

import fcntl
import os
import plistlib
import pwd
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from unittest.mock import patch

import pytest
from octomate_cli.main import app
from octomate_cli.serve import DatabaseBackup, PlistService
from typer.testing import CliRunner


@dataclass
class Operations:
    database: Path
    loaded: bool = False
    dirty: bool = False
    branch: str = "main"
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

    def git(
        self, arguments: list[str], *, cwd: Path, env: dict[str, str], text: bool
    ) -> str:
        assert cwd.is_dir()
        assert text
        assert "OCTOMATE_DB_URL" in env
        assert "GIT_INDEX_FILE" not in env
        if arguments[1] == "branch":
            return self.branch
        if arguments[1] == "status":
            return " M main.py" if self.dirty else ""
        assert arguments[1] == "rev-parse"
        if arguments[2] == "FETCH_HEAD" and self.ahead:
            return "remote-commit"
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
        if arguments == ["git", "pull", "--ff-only", "origin", "main"]:
            step = "pull"
        else:
            assert arguments == [
                "uv",
                "sync",
                "--frozen",
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
    database = tmp_path / "shared" / "octomate.db"
    plist = tmp_path / "server.plist"
    plist.write_bytes(
        plistlib.dumps(
            {
                "Label": "io.octomate.server",
                "WorkingDirectory": str(directory),
                "UserName": pwd.getpwuid(os.getuid()).pw_name,
                "ProgramArguments": [
                    str(directory / ".venv/bin/octomate"),
                    "serve",
                ],
                "KeepAlive": True,
                "EnvironmentVariables": {
                    "PATH": "/usr/bin:/bin",
                    "OCTOMATE_HOME": str(tmp_path / "config"),
                    "OCTOMATE_DB_URL": f"sqlite+aiosqlite:///{database}",
                },
            }
        )
    )
    operations = Operations(database=database)
    monkeypatch.setattr(sys, "platform", "darwin")
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


def test_start_migrates_before_loading_service(
    service: tuple[Path, Operations],
) -> None:
    plist, operations = service
    result = CliRunner().invoke(app, ["serve", "--plist", str(plist)])
    assert result.exit_code == 0, result.output
    assert operations.events == [
        "check",
        "disable",
        "backup",
        "migrate",
        "enable",
        "bootstrap",
        "verify",
    ]
    assert operations.loaded


def test_start_loaded_service_only_verifies(service: tuple[Path, Operations]) -> None:
    plist, operations = service
    operations.loaded = True
    result = CliRunner().invoke(app, ["serve", "--plist", str(plist)])
    assert result.exit_code == 0, result.output
    assert operations.events == ["check", "verify"]


@pytest.mark.parametrize(
    "options",
    [
        ["--host", "127.0.0.1"],
        ["--port", "9000"],
        ["--reload"],
        ["--tmux"],
        ["--session", "test-server"],
    ],
)
def test_managed_serve_refuses_foreground_options(
    service: tuple[Path, Operations], options: list[str]
) -> None:
    plist, operations = service
    result = CliRunner().invoke(app, ["serve", "--plist", str(plist), *options])
    assert result.exit_code == 2
    assert "--plist uses the service configuration" in result.output
    assert operations.events == []


def test_upgrade_orders_stop_backup_pull_sync_migrate_start(
    service: tuple[Path, Operations],
) -> None:
    plist, operations = service
    operations.loaded = True
    result = CliRunner().invoke(app, ["upgrade", "--plist", str(plist)])
    assert result.exit_code == 0, result.output
    assert operations.events == [
        "check",
        "disable",
        "bootout",
        "backup",
        "pull",
        "sync",
        "migrate",
        "enable",
        "bootstrap",
        "verify",
    ]


@pytest.mark.parametrize("failure", ["backup", "pull", "sync", "migrate", "verify"])
def test_failed_upgrade_stays_disabled(
    service: tuple[Path, Operations], failure: str
) -> None:
    plist, operations = service
    operations.loaded = True
    operations.fail = failure
    result = CliRunner().invoke(app, ["upgrade", "--plist", str(plist)])
    assert result.exit_code == 1
    assert not operations.loaded
    assert "remains disabled" in result.output
    after_failure = operations.events[operations.events.index(failure) + 1 :]
    assert after_failure == (
        ["disable", "bootout"] if failure == "verify" else ["disable"]
    )


@pytest.mark.parametrize(("dirty", "branch"), [(True, "main"), (False, "feature")])
def test_upgrade_refuses_unreviewed_checkout(
    service: tuple[Path, Operations], dirty: bool, branch: str
) -> None:
    plist, operations = service
    operations.loaded = True
    operations.dirty = dirty
    operations.branch = branch
    result = CliRunner().invoke(app, ["upgrade", "--plist", str(plist)])
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
        result = CliRunner().invoke(app, ["upgrade", "--plist", str(plist)])
    assert result.exit_code == 2
    assert "Another server operation" in result.output
    assert operations.events == []


def test_upgrade_refuses_local_commits_ahead_of_remote(
    service: tuple[Path, Operations],
) -> None:
    plist, operations = service
    operations.ahead = True
    result = CliRunner().invoke(app, ["upgrade", "--plist", str(plist)])
    assert result.exit_code == 1
    assert "differs from the fetched main" in result.output
    assert "sync" not in operations.events
    assert "migrate" not in operations.events


def test_foreground_run_passes_bind_and_reload_options(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OCTOMATE__PORT", "8000")
    with patch("uvicorn.run") as run:
        result = CliRunner().invoke(
            app,
            ["serve", "--host", "127.0.0.1", "--port", "9000", "--reload"],
        )
    assert result.exit_code == 0, result.output
    run.assert_called_once()
    assert run.call_args.args == ("main:create_app",)
    assert run.call_args.kwargs["factory"] is True
    assert run.call_args.kwargs["host"] == "127.0.0.1"
    assert run.call_args.kwargs["port"] == 9000
    assert run.call_args.kwargs["reload"] is True
    assert os.environ["OCTOMATE__PORT"] == "9000"


def test_tmux_launches_the_serve_command(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    executable = tmp_path / ".venv/bin/octomate"
    monkeypatch.setattr(sys, "argv", [str(executable)])
    monkeypatch.delenv("TMUX", raising=False)
    with (
        patch("shutil.which", return_value="/usr/bin/tmux"),
        patch(
            "subprocess.run",
            side_effect=[
                subprocess.CompletedProcess(["tmux"], 1),
                subprocess.CompletedProcess(["tmux"], 0),
                subprocess.CompletedProcess(["tmux"], 0),
            ],
        ) as run,
    ):
        result = CliRunner().invoke(
            app,
            [
                "serve",
                "--tmux",
                "--session",
                "test-server",
                "--host",
                "127.0.0.1",
                "--port",
                "9000",
                "--reload",
            ],
        )
    assert result.exit_code == 0, result.output
    assert run.call_count == 3
    assert run.call_args_list[1].args[0] == [
        "tmux",
        "new-session",
        "-d",
        "-s",
        "test-server",
        "-c",
        str(tmp_path),
        str(executable),
        "serve",
        "--host",
        "127.0.0.1",
        "--port",
        "9000",
        "--reload",
    ]
    assert run.call_args_list[2].args[0] == [
        "tmux",
        "attach-session",
        "-t",
        "test-server",
    ]


def test_server_group_is_removed() -> None:
    result = CliRunner().invoke(app, ["server"])
    assert result.exit_code == 2
    assert "No such command 'server'" in result.output


@pytest.mark.parametrize(
    "command",
    [[], ["serve"], ["upgrade"]],
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
