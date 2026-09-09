"""CLI upgrades must never mutate a service or an unowned environment."""

from __future__ import annotations

import subprocess
import sys
from importlib.metadata import PackageNotFoundError
from pathlib import Path
from unittest.mock import Mock

import pytest
from octomate_cli import cli
from octomate_cli.main import app
from typer.testing import CliRunner

runner = CliRunner()


@pytest.fixture
def installation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    prefix = tmp_path / "tools" / "octomate-cli"
    (prefix / "bin").mkdir(parents=True)
    (prefix / "bin" / "octomate").touch()
    (prefix / "uv-receipt.toml").write_text(
        '[tool]\nrequirements = [{name="octomate-cli"}]'
    )
    installed = Mock()
    installed.read_text.return_value = "uv\n"
    installed.locate_file.return_value = prefix / "lib" / "site-packages"
    monkeypatch.setattr(
        cli,
        "distribution",
        Mock(side_effect=[PackageNotFoundError("octomate"), installed]),
    )
    monkeypatch.setattr(sys, "prefix", str(prefix))
    monkeypatch.setattr(cli.shutil, "which", Mock(return_value="/tools/uv"))
    return prefix


def test_upgrade_only_the_owned_cli_and_read_its_new_version(
    installation: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run = Mock(
        side_effect=[
            subprocess.CompletedProcess([], 0, str(installation.parent)),
            subprocess.CompletedProcess([], 0),
            subprocess.CompletedProcess([], 0, "octomate-cli 0.1.0\n"),
        ]
    )
    monkeypatch.setattr(cli.subprocess, "run", run)

    result = runner.invoke(app, ["upgrade"])

    assert result.exit_code == 0, result.output
    assert "octomate-cli 0.1.0" in result.output
    assert [call.args[0] for call in run.call_args_list] == [
        ["/tools/uv", "tool", "dir"],
        ["/tools/uv", "tool", "upgrade", "octomate-cli"],
        [str(installation / "bin" / "octomate"), "--version"],
    ]


def test_service_environment_refused_before_any_subprocess(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(cli, "distribution", Mock(return_value=Mock()))
    run = Mock()
    monkeypatch.setattr(cli.subprocess, "run", run)

    result = runner.invoke(app, ["upgrade"])

    assert result.exit_code == 1
    assert "contains the Octomate service" in result.output
    run.assert_not_called()


@pytest.mark.parametrize("mismatch", ["prefix", "receipt", "metadata"])
def test_unowned_uv_installation_is_never_upgraded(
    installation: Path, monkeypatch: pytest.MonkeyPatch, mismatch: str
) -> None:
    if mismatch == "prefix":
        monkeypatch.setattr(sys, "prefix", str(installation.parent / "other"))
    elif mismatch == "receipt":
        (installation / "uv-receipt.toml").unlink()
    else:
        installed = Mock()
        installed.read_text.return_value = "uv"
        installed.locate_file.return_value = installation.parent / "foreign"
        monkeypatch.setattr(
            cli,
            "distribution",
            Mock(side_effect=[PackageNotFoundError("octomate"), installed]),
        )
    run = Mock(
        side_effect=[
            subprocess.CompletedProcess([], 0, str(installation.parent)),
            subprocess.CompletedProcess([], 0, str(installation.parent / "cache")),
        ]
    )
    monkeypatch.setattr(cli.subprocess, "run", run)

    result = runner.invoke(app, ["upgrade"])

    assert result.exit_code == 1
    assert "Cannot verify standalone uv tool ownership" in result.output
    assert run.call_count == 2


def test_temporary_uvx_invocation_gets_refresh_guidance(
    installation: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cache = installation.parent / "cache"
    monkeypatch.setattr(sys, "prefix", str(cache / "archive-v0" / "temporary"))
    run = Mock(
        side_effect=[
            subprocess.CompletedProcess([], 0, str(installation.parent)),
            subprocess.CompletedProcess([], 0, str(cache)),
        ]
    )
    monkeypatch.setattr(cli.subprocess, "run", run)

    result = runner.invoke(app, ["upgrade"])

    assert result.exit_code == 1
    assert "temporary uv environment" in result.output
    assert "uvx --refresh --from octomate-cli octomate" in result.output
    assert run.call_count == 2


@pytest.mark.parametrize("installer", ["pip", "unknown"])
def test_other_installers_are_reported_without_mutation(
    installation: Path, monkeypatch: pytest.MonkeyPatch, installer: str
) -> None:
    installed = Mock()
    installed.read_text.return_value = installer
    monkeypatch.setattr(
        cli,
        "distribution",
        Mock(side_effect=[PackageNotFoundError("octomate"), installed]),
    )
    run = Mock()
    monkeypatch.setattr(cli.subprocess, "run", run)

    result = runner.invoke(app, ["upgrade"])

    assert result.exit_code == 1
    assert f"installer: {installer}" in result.output
    expected = "-m pip install --upgrade" if installer == "pip" else "package manager"
    assert expected in result.output
    run.assert_not_called()


def test_installer_failure_is_not_reported_as_success(
    installation: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run = Mock(
        side_effect=[
            subprocess.CompletedProcess([], 0, str(installation.parent)),
            subprocess.CalledProcessError(2, ["uv", "tool", "upgrade"]),
        ]
    )
    monkeypatch.setattr(cli.subprocess, "run", run)

    result = runner.invoke(app, ["upgrade"])

    assert result.exit_code == 1
    assert "CLI upgrade failed" in result.output
    assert run.call_count == 2


def test_missing_executable_is_refused_before_upgrade(
    installation: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (installation / "bin" / "octomate").unlink()
    run = Mock(
        return_value=subprocess.CompletedProcess([], 0, str(installation.parent))
    )
    monkeypatch.setattr(cli.subprocess, "run", run)

    result = runner.invoke(app, ["upgrade"])

    assert result.exit_code == 1
    assert "uv tool install --reinstall octomate-cli" in result.output
    assert run.call_count == 1


def test_missing_uv_reports_literal_path_without_color_or_subprocess(
    installation: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    prefix = installation.parent / "[red]cli[/red]"
    monkeypatch.setattr(sys, "prefix", str(prefix))
    monkeypatch.setenv("NO_COLOR", "1")
    monkeypatch.setattr(cli.shutil, "which", Mock(return_value=None))
    run = Mock()
    monkeypatch.setattr(cli.subprocess, "run", run)

    result = runner.invoke(app, ["upgrade"])

    assert result.exit_code == 1
    assert str(prefix) in result.output
    assert "\x1b[" not in result.output
    assert "uv tool install octomate-cli" in result.output
    run.assert_not_called()
