"""GUI service controls preserve launch context and keep inspection read-only."""

from __future__ import annotations

import os
import plistlib
import pwd
import subprocess
from pathlib import Path
from unittest.mock import Mock

import pytest
import typer
from octomate_cli import service as service_cli
from octomate_cli.service import service_typer
from rich.console import Console
from typer.testing import CliRunner

runner = CliRunner()


@pytest.fixture(autouse=True)
def isolate_processes(monkeypatch: pytest.MonkeyPatch) -> None:
    for owner, name in (
        (service_cli.subprocess, "run"),
        (service_cli.subprocess, "check_output"),
        (service_cli.os, "getpgid"),
        (service_cli.os, "killpg"),
    ):
        monkeypatch.setattr(owner, name, Mock(side_effect=AssertionError(name)))
    monkeypatch.setattr(service_cli.time, "sleep", Mock())


@pytest.fixture
def service(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> service_cli.PlistService:
    account = pwd.getpwuid(os.getuid())
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    instance = service_cli.PlistService(
        Label="io.octomate.server",
        WorkingDirectory=tmp_path,
        ProgramArguments=[str(checkout / ".venv/bin/octomate"), "service", "serve"],
        EnvironmentVariables={
            "HOME": account.pw_dir,
            "USER": account.pw_name,
            "LOGNAME": account.pw_name,
            "PATH": "/usr/bin:/bin",
            "OCTOMATE_HOME": str(tmp_path / "[red]config[/red]"),
            "OCTOMATE_DB_URL": "sqlite+aiosqlite:///:memory:",
        },
        StandardOutPath=tmp_path / "stdout.log",
        StandardErrorPath=tmp_path / "stderr.log",
        LimitLoadToSessionType="Aqua",
        KeepAlive=True,
    )
    plist = tmp_path / "service.plist"
    plist.write_bytes(
        plistlib.dumps(
            instance.model_dump(mode="json", by_alias=True, exclude_none=True)
        )
    )
    monkeypatch.setattr(service_cli.PlistService, "path", staticmethod(lambda: plist))
    monkeypatch.setattr(service_cli.sys, "platform", "darwin")
    monkeypatch.setattr(
        service_cli,
        "console",
        Console(stderr=True, width=240, highlight=False, markup=False),
    )
    return instance


def test_gui_definition_resolves_checkout_separately_from_working_directory(
    service: service_cli.PlistService,
) -> None:
    loaded = service_cli.PlistService.load()

    assert loaded.checkout == service.checkout
    assert loaded.directory == service.root
    assert loaded.target == f"gui/{os.getuid()}/io.octomate.server"
    assert loaded.environment == service.environment


@pytest.mark.parametrize(
    "invalid",
    [
        "root",
        "UserName",
        "GroupName",
        "directory",
        "log",
        "executable",
        "module_runner",
        "HOME",
    ],
)
def test_invalid_launch_context_is_refused(
    service: service_cli.PlistService, monkeypatch: pytest.MonkeyPatch, invalid: str
) -> None:
    path = service.path()
    payload = plistlib.loads(path.read_bytes())
    if invalid == "root":
        monkeypatch.setattr(service_cli.os, "getuid", Mock(return_value=0))
    elif invalid in {"UserName", "GroupName"}:
        payload[invalid] = "someone"
    elif invalid == "directory":
        payload["WorkingDirectory"] = "relative"
    elif invalid == "log":
        payload["StandardOutPath"] = "relative.log"
    elif invalid == "executable":
        payload["ProgramArguments"] = ["octomate", "serve"]
    elif invalid == "module_runner":
        payload["ProgramArguments"] = [
            str(service.checkout / ".venv/bin/python"),
            "-m",
            "octomate",
        ]
    else:
        del payload["EnvironmentVariables"]["HOME"]
    path.write_bytes(plistlib.dumps(payload))

    with pytest.raises(typer.BadParameter):
        service_cli.PlistService.load()


def test_gui_control_is_unprivileged(
    service: service_cli.PlistService, monkeypatch: pytest.MonkeyPatch
) -> None:
    run = Mock(return_value=subprocess.CompletedProcess([], 0))
    monkeypatch.setattr(service_cli.subprocess, "run", run)

    service.require_gui()
    service.launchctl("enable", service.target)
    service.launchctl("bootstrap", service.domain, str(service.path()))

    assert [call.args[0] for call in run.call_args_list] == [
        ["/bin/launchctl", "print", service.domain],
        ["/bin/launchctl", "enable", service.target],
        ["/bin/launchctl", "bootstrap", service.domain, str(service.path())],
    ]


def test_absent_desktop_session_stops_before_service_or_database_mutation(
    service: service_cli.PlistService, monkeypatch: pytest.MonkeyPatch
) -> None:
    run = Mock(return_value=subprocess.CompletedProcess([], 113))
    monkeypatch.setattr(service_cli.subprocess, "run", run)

    result = runner.invoke(service_typer, ["start"])

    assert result.exit_code == 1
    assert "Log into the desktop first" in result.output
    assert [call.args[0] for call in run.call_args_list] == [
        ["/bin/launchctl", "print", service.domain]
    ]


@pytest.mark.parametrize("timeout", [False, True])
def test_stop_waits_for_children_with_a_bounded_deadline(
    service: service_cli.PlistService, monkeypatch: pytest.MonkeyPatch, timeout: bool
) -> None:
    run = Mock(return_value=subprocess.CompletedProcess([], 0, "pid = 123\n"))
    monkeypatch.setattr(service_cli.subprocess, "run", run)
    monkeypatch.setattr(service_cli.os, "getpgid", Mock(return_value=456))
    killpg = Mock(side_effect=[None, None] if timeout else [None, ProcessLookupError])
    monkeypatch.setattr(service_cli.os, "killpg", killpg)
    monkeypatch.setattr(service_cli.time, "monotonic", Mock(side_effect=[0, 0, 31]))

    if timeout:
        with pytest.raises(TimeoutError, match="30 seconds"):
            service.stop()
    else:
        service.stop()

    assert [call.args for call in killpg.call_args_list] == [(456, 0), (456, 0)]
    assert [call.args[0] for call in run.call_args_list] == [
        ["/bin/launchctl", "disable", service.target],
        ["/bin/launchctl", "print", service.target],
        ["/bin/launchctl", "print", service.target],
        ["/bin/launchctl", "bootout", service.target],
    ]


def test_status_is_read_only_and_preserves_literal_markup(
    service: service_cli.PlistService, monkeypatch: pytest.MonkeyPatch
) -> None:
    run = Mock(return_value=subprocess.CompletedProcess([], 0, "pid = 123\n"))
    output = Mock(side_effect=['"io.octomate.server" => true', "abc123"])
    monkeypatch.setattr(service_cli.subprocess, "run", run)
    monkeypatch.setattr(service_cli.subprocess, "check_output", output)
    monkeypatch.setenv("NO_COLOR", "1")

    result = runner.invoke(service_typer, ["status"])

    assert result.exit_code == 0, result.output
    assert "[red]config[/red]" in result.output
    assert "123" in result.output
    assert "\x1b[" not in result.output
    assert [call.args[0][1] for call in run.call_args_list] == ["print", "print"]
    assert [call.args[0][1] for call in output.call_args_list] == [
        "print-disabled",
        "rev-parse",
    ]
    assert not (service.root / "control").exists()
    assert not (service.root / "logs").exists()


@pytest.mark.parametrize("follow", [False, True])
def test_logs_only_tail_the_configured_paths(
    service: service_cli.PlistService, monkeypatch: pytest.MonkeyPatch, follow: bool
) -> None:
    run = Mock(return_value=subprocess.CompletedProcess([], 0))
    monkeypatch.setattr(service_cli.subprocess, "run", run)

    result = runner.invoke(service_typer, ["logs", *(["--follow"] if follow else [])])

    assert result.exit_code == 0, result.output
    run.assert_called_once_with(
        [
            "/usr/bin/tail",
            "-n",
            "100",
            *(["-f"] if follow else []),
            "--",
            str(service.stdout),
            str(service.stderr),
        ],
        check=True,
    )
    assert not (service.root / "control").exists()


def test_verify_only_runs_read_only_maintenance_checks(
    service: service_cli.PlistService, monkeypatch: pytest.MonkeyPatch
) -> None:
    run = Mock(return_value=subprocess.CompletedProcess([], 0, "verified"))
    monkeypatch.setattr(service_cli.subprocess, "run", run)

    result = runner.invoke(service_typer, ["verify"])

    assert result.exit_code == 0, result.output
    assert "Not checked: Claude login, connectors and plugins" in result.output
    commands = [call.args[0] for call in run.call_args_list]
    assert commands[:2] == [
        ["/bin/launchctl", "print", service.domain],
        ["/bin/launchctl", "print", service.target],
    ]
    assert commands[2:] == [
        [
            str(service.checkout / ".venv/bin/python"),
            "-m",
            "octomate_cli.deployment",
            "ready",
        ],
        [
            str(service.checkout / ".venv/bin/python"),
            "-m",
            "octomate_cli.deployment",
            "verify",
        ],
    ]
    assert not (service.root / "control").exists()
    assert not (service.root / "logs").exists()
