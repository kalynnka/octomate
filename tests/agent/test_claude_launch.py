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

import pytest
from octomate_cli import launch as launch_module
from octomate_cli.claude import CLAUDE_HOOK_PATH
from octomate_cli.config import CLISettings, project_config_path, user_config_path
from octomate_cli.hooks import LAUNCH_SCRIPT
from octomate_cli.launch import OCTOMATE_URL_ENV as LAUNCH_URL_ENV

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
    args: list[str], payload: Mapping[str, object], env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(LAUNCH_SCRIPT), *args],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        # Controlled: the suite may run in a shell that exports OCTOMATE_CLI_URL itself,
        # and these tests are about what the script resolves, not what leaks in. HOME
        # and cwd pinned to nowhere so the developer's real cli.toml never steers a
        # test in either scope.
        env={"PATH": "/usr/bin:/bin", "HOME": "/nonexistent", **(env or {})},
        cwd="/",
    )


def wait_for(path: Path, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while not path.exists():
        if time.monotonic() > deadline:
            raise AssertionError(f"{path} never appeared")
        time.sleep(0.05)


def test_launch_spawns_a_detached_tail_and_stays_silent(tmp_path: Path) -> None:
    binary, args_file = recorder(tmp_path)
    result = launch(["--url", STREAM_URL, "--octomate", str(binary)], EVENT)

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


def test_a_codex_subagent_stop_hands_the_child_path_to_the_codex_tail(
    tmp_path: Path,
) -> None:
    """`--agent codex` routes the spawn to `octomate codex tail`, and a SubagentStop
    event's `agent_transcript_path` rides along as `--agent-path`: the running tail
    cannot tell which sibling rollout is a child of its session, so the hook that
    knows hands it the path."""
    binary, args_file = recorder(tmp_path)
    codex_stream = "ws://127.0.0.1:9999/hooks/codex/stream"
    result = launch(
        ["--url", codex_stream, "--agent", "codex", "--octomate", str(binary)],
        {
            "hook_event_name": "SubagentStop",
            "session_id": "s1",
            "transcript_path": "/laptop/.codex/sessions/2026/08/13/rollout-parent.jsonl",
            "cwd": "/repo",
            "agent_transcript_path": (
                "/laptop/.codex/sessions/2026/08/13/rollout-child.jsonl"
            ),
        },
    )

    assert result.returncode == 0
    assert result.stdout == ""
    wait_for(args_file)
    assert args_file.read_text().split() == [
        "codex",
        "tail",
        "--session",
        "s1",
        "--path",
        "/laptop/.codex/sessions/2026/08/13/rollout-parent.jsonl",
        "--url",
        codex_stream,
        "--cwd",
        "/repo",
        "--agent-path",
        "/laptop/.codex/sessions/2026/08/13/rollout-child.jsonl",
    ]


def test_an_event_naming_no_transcript_spawns_nothing(tmp_path: Path) -> None:
    binary, args_file = recorder(tmp_path)
    result = launch(
        ["--url", STREAM_URL, "--octomate", str(binary)],
        {"hook_event_name": "UserPromptSubmit", "session_id": "s1"},
    )

    assert result.returncode == 0
    assert result.stdout == ""
    time.sleep(0.2)  # absence cannot be awaited; give a wrong spawn time to land
    assert not args_file.exists()


def test_the_stream_url_derives_from_the_environment(tmp_path: Path) -> None:
    """The installed command carries only `--path`; the stream address comes from
    OCTOMATE_CLI_URL when the hook fires — `https` base, `wss` stream — so the launcher
    follows the same environment switch the forwarding hooks do."""
    binary, args_file = recorder(tmp_path)
    result = launch(
        ["--path", CLAUDE_HOOK_PATH, "--octomate", str(binary)],
        EVENT,
        env={CLISettings.env("url"): "https://minidock.example:8443"},
    )

    assert result.returncode == 0
    assert result.stdout == ""
    wait_for(args_file)
    arguments = args_file.read_text().split()
    assert (
        arguments[arguments.index("--url") + 1]
        == "wss://minidock.example:8443/hooks/claude/stream"
    )


def test_without_a_target_nothing_spawns_and_nothing_is_said(tmp_path: Path) -> None:
    """No pin and no OCTOMATE_CLI_URL: the emit hook on the same event already complained
    on stderr, and a tail with no server to call would only retry into the void."""
    binary, args_file = recorder(tmp_path)
    result = launch(["--path", CLAUDE_HOOK_PATH, "--octomate", str(binary)], EVENT)

    assert result.returncode == 0
    assert result.stdout == ""
    time.sleep(0.2)  # absence cannot be awaited; give a wrong spawn time to land
    assert not args_file.exists()


def test_the_stream_url_derives_from_the_config_file(tmp_path: Path) -> None:
    """The file backstop, for launch paths that never sourced a shell profile — the
    same floor the emit hook stands on."""
    binary, args_file = recorder(tmp_path)
    home = tmp_path / "home"
    (home / ".config" / "octomate").mkdir(parents=True)
    (home / ".config" / "octomate" / "cli.toml").write_text(
        'url = "http://minidock.local:8000"\n'
    )
    result = launch(
        ["--path", CLAUDE_HOOK_PATH, "--octomate", str(binary)],
        EVENT,
        env={"HOME": str(home)},
    )

    assert result.returncode == 0
    assert result.stdout == ""
    wait_for(args_file)
    arguments = args_file.read_text().split()
    assert (
        arguments[arguments.index("--url") + 1]
        == "ws://minidock.local:8000/hooks/claude/stream"
    )


def test_its_duplicated_names_still_match_the_canonical_ones(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """launch.py repeats the variable name and the config path as literals because it
    must not import the package; this is what stops the copies drifting."""
    assert LAUNCH_URL_ENV == CLISettings.env("url")
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.chdir(tmp_path)
    assert launch_module.config_files() == (project_config_path(), user_config_path())


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
