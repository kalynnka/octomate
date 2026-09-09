from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest
from click import unstyle
from octomate_cli.main import app
from typer.testing import CliRunner


def test_service_serve_help_is_available() -> None:
    result = subprocess.run(
        [sys.executable, "-m", "octomate_cli.main", "service", "serve", "--help"],
        capture_output=True,
        text=True,
        check=True,
    )
    assert "--port" in unstyle(result.stdout)
    assert "--reload" in unstyle(result.stdout)


def test_foreground_run_passes_bind_and_reload_options(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OCTOMATE__PORT", "8000")
    with patch("uvicorn.run") as run:
        result = CliRunner().invoke(
            app,
            ["service", "serve", "--host", "127.0.0.1", "--port", "9000", "--reload"],
        )
    assert result.exit_code == 0, result.output
    run.assert_called_once()
    assert run.call_args.args == ("octomate.app:create_app",)
    assert run.call_args.kwargs["factory"] is True
    assert run.call_args.kwargs["host"] == "127.0.0.1"
    assert run.call_args.kwargs["port"] == 9000
    assert run.call_args.kwargs["reload"] is True
    assert os.environ["OCTOMATE__PORT"] == "9000"


def test_tmux_launches_service_serve(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    executable = tmp_path / ".venv/bin/python"
    monkeypatch.setattr(sys, "executable", str(executable))
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
                "service",
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
        "-m",
        "octomate_cli.main",
        "service",
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
