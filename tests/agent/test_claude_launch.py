"""The launcher hook's script: the command Claude runs on every prompt to ensure a
transcript tail is up. It is on the blocking path of the turn, so it must detach,
stay silent on stdout (a `UserPromptSubmit` hook's stdout is injected into the turn's
context), and never import the package."""

from __future__ import annotations

import json
import subprocess
import sys
import time
from collections.abc import Mapping
from pathlib import Path

from octomate.tentacles.agents.claude.typer import LAUNCH_SCRIPT

STREAM_URL = "ws://127.0.0.1:9999/hooks/claude/stream"
EVENT = {
    "hook_event_name": "UserPromptSubmit",
    "session_id": "s1",
    "transcript_path": "/laptop/.claude/projects/-repo/s1.jsonl",
    "cwd": "/repo",
}


def recorder(tmp_path: Path) -> tuple[Path, Path]:
    """A stand-in octomate binary that records its argv and exits."""
    args_file = tmp_path / "args.txt"
    binary = tmp_path / "octomate"
    binary.write_text(f'#!/bin/sh\necho "$@" > {args_file}\n')
    binary.chmod(0o755)
    return binary, args_file


def launch(
    binary: Path, payload: Mapping[str, object]
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            str(LAUNCH_SCRIPT),
            "--url",
            STREAM_URL,
            "--octomate",
            str(binary),
        ],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
    )


def wait_for(path: Path, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while not path.exists():
        if time.monotonic() > deadline:
            raise AssertionError(f"{path} never appeared")
        time.sleep(0.05)


def test_launch_spawns_a_detached_tail_and_stays_silent(tmp_path: Path) -> None:
    binary, args_file = recorder(tmp_path)
    result = launch(binary, EVENT)

    assert result.returncode == 0
    # Anything printed here would be injected into the turn's context.
    assert result.stdout == ""

    wait_for(args_file)  # the spawn is detached, so it may land after launch exits
    assert args_file.read_text().split() == [
        "claude",
        "tail",
        "--session",
        "s1",
        "--path",
        "/laptop/.claude/projects/-repo/s1.jsonl",
        "--url",
        STREAM_URL,
        "--cwd",
        "/repo",
    ]


def test_an_event_naming_no_transcript_spawns_nothing(tmp_path: Path) -> None:
    binary, args_file = recorder(tmp_path)
    result = launch(binary, {"hook_event_name": "UserPromptSubmit", "session_id": "s1"})

    assert result.returncode == 0
    assert result.stdout == ""
    time.sleep(0.2)  # absence cannot be awaited; give a wrong spawn time to land
    assert not args_file.exists()


def test_bad_usage_fails_loudly() -> None:
    result = subprocess.run(
        [sys.executable, str(LAUNCH_SCRIPT), "--url", STREAM_URL],
        input="{}",
        capture_output=True,
        text=True,
    )
    assert result.returncode == 2
    assert "usage" in result.stderr


def test_the_script_never_imports_the_octomate_package() -> None:
    """Why this script exists at all: importing the package builds `Octomate` (~1.9s),
    and Claude blocks on this hook once a turn. Run by path with stdlib imports only
    it stays ~50ms; an `octomate` import here would silently hand that cost back."""
    probe = (
        "import importlib.util, sys;"
        f"spec = importlib.util.spec_from_file_location('launch', {str(LAUNCH_SCRIPT)!r});"
        "module = importlib.util.module_from_spec(spec);"
        "spec.loader.exec_module(module);"
        "print(any(name == 'octomate' or name.startswith('octomate.') "
        "for name in sys.modules))"
    )
    result = subprocess.run(
        [sys.executable, "-c", probe], capture_output=True, text=True, check=True
    )
    assert result.stdout.strip() == "False"
