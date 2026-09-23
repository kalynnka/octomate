"""The Codex hook's emit script: the command Codex runs, and the HTTP it speaks.

Codex has no `http` hook handler, so a native session reaches Octomate through a
command. These pin the contract that command must keep — it is on the blocking path of
every turn.
"""

from __future__ import annotations

import json
import subprocess
import sys
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, HTTPServer
from io import StringIO
from pathlib import Path
from threading import Thread
from unittest.mock import patch

import pytest
from octomate_cli import emit as emit_module
from octomate_cli.config import CLISettings, project_config_path, user_config_path
from octomate_cli.emit import (
    CODEX_HOOK_PATH,
    HOOK_TIMEOUT,
    OCTOMATE_URL_ENV,
    TOKEN_ENV,
)
from octomate_cli.tentacles.codex import CODEX_HOOK_PATH as CANONICAL_CODEX_HOOK_PATH
from octomate_cli.tentacles.codex.hooks import HOOK_TIMEOUT as CANONICAL_HOOK_TIMEOUT
from octomate_cli.tentacles.hooks import EMIT_SCRIPT
from openai_codex import CodexError
from openai_codex.generated.v2_all import ThreadReadResponse
from pydantic import ValidationError

SECRET = "the-hook-token"
# Transport tests use a child event so they never launch a real Codex process.
PAYLOAD = {"hook_event_name": "SubagentStop", "session_id": "s1", "turn_id": "t1"}


class Received:
    def __init__(self) -> None:
        self.body: dict[str, object] | None = None
        self.authorization: str | None = None


@pytest.fixture
def router() -> Iterator[tuple[str, Received]]:
    """A stand-in for Octomate's hook router, recording what the script delivers."""
    received = Received()

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            length = int(self.headers["Content-Length"])
            received.body = json.loads(self.rfile.read(length))
            received.authorization = self.headers["Authorization"]
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b"{}")

        def log_message(self, format: str, *args: object) -> None:
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_port}/hooks/codex", received
    server.shutdown()


def emit[T](
    args: list[str], env: dict[str, str], payload: dict[str, T] | None = None
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(EMIT_SCRIPT), *args],
        input=json.dumps(payload if payload is not None else PAYLOAD),
        capture_output=True,
        text=True,
        # HOME and cwd pinned to nowhere so the developer's real cli.toml never
        # steers a test in either scope (HOME unset, Python falls back to the passwd
        # database); the file-backstop tests pass their own.
        env={"PATH": "/usr/bin:/bin", "HOME": "/nonexistent", **env},
        cwd="/",
    )


def base_of(url: str) -> str:
    """The server base a session's OCTOMATE_CLI_URL would carry, from the fixture's URL."""
    return url.removesuffix(CODEX_HOOK_PATH)


def test_the_payload_is_delivered_bearing_the_hook_credential(
    router: tuple[str, Received],
) -> None:
    url, received = router
    result = emit(["--path", CODEX_HOOK_PATH, "--url", url], {TOKEN_ENV: SECRET})

    assert result.returncode == 0
    assert received.body == PAYLOAD
    assert received.authorization == f"Bearer {SECRET}"
    # Codex reads stdout as the hook's decision; an observer decides nothing.
    assert result.stdout.strip() == "{}"


def test_an_undriven_session_is_not_marked(router: tuple[str, Received]) -> None:
    url, received = router
    emit(["--path", CODEX_HOOK_PATH, "--url", url], {TOKEN_ENV: SECRET})

    assert received.body is not None
    assert "octomate_driven" not in received.body


def test_forwarding_preserves_unmodeled_fields_and_explicit_nulls(
    router: tuple[str, Received],
) -> None:
    url, received = router
    payload = {
        **PAYLOAD,
        "agent_id": None,
        "runtime_data": {"nested": [1, "原样", None, {"enabled": True}]},
        "last_assistant_message": "done",
    }

    result = emit(
        ["--path", CODEX_HOOK_PATH, "--url", url], {TOKEN_ENV: SECRET}, payload
    )

    assert result.returncode == 0
    assert received.body == payload


@pytest.mark.parametrize(
    "raw",
    [
        "[]",
        '{"hook_event_name": "Stop"}',
        '{"hook_event_name": "Stop", "session_id": 42}',
        '{"hook_event_name": "Stop", "session_id": "s1", "agent_id": []}',
    ],
)
def test_invalid_hook_payloads_fail_before_sdk_or_http_calls(
    monkeypatch: pytest.MonkeyPatch, raw: str
) -> None:
    monkeypatch.setattr(sys, "stdin", StringIO(raw))
    with (
        patch.object(emit_module, "CodexClient") as client,
        patch.object(emit_module.urllib.request, "urlopen") as post,
        pytest.raises(ValidationError),
    ):
        emit_module.main("http://localhost/hooks/codex", SECRET)

    client.assert_not_called()
    post.assert_not_called()


@pytest.mark.parametrize("event", ["SessionStart", "UserPromptSubmit", "Stop"])
@pytest.mark.parametrize("name", ["修复 session names", None, "", "  "])
def test_codex_hooks_read_the_name_through_the_sdk(
    router: tuple[str, Received],
    monkeypatch: pytest.MonkeyPatch,
    event: str,
    name: str | None,
) -> None:
    url, received = router
    payload = {**PAYLOAD, "hook_event_name": event}
    monkeypatch.setattr(sys, "stdin", StringIO(json.dumps(payload)))
    response = ThreadReadResponse.model_validate(
        {
            "thread": {
                "id": "s1",
                "sessionId": "s1",
                "name": name,
                "agentNickname": "Kepler",
                "cliVersion": "0.147.0",
                "createdAt": 0,
                "updatedAt": 0,
                "cwd": "/client/project",
                "ephemeral": False,
                "modelProvider": "openai",
                "preview": "the opening line",
                "source": "cli",
                "status": {"type": "notLoaded"},
                "turns": [],
            }
        }
    )
    with patch.object(emit_module, "CodexClient", autospec=True) as factory:
        client = factory.return_value.__enter__.return_value
        client.thread_read.return_value = response

        assert emit_module.main(url, SECRET) == 0

        client.initialize.assert_called_once_with()
        client.thread_read.assert_called_once_with("s1", include_turns=False)
        client.thread_resume.assert_not_called()
        client.thread_start.assert_not_called()
        factory.return_value.__exit__.assert_called_once()
    expected = {**payload, "session_name": name} if name and name.strip() else payload
    assert received.body == expected
    assert received.authorization == f"Bearer {SECRET}"


@pytest.mark.parametrize(
    "error", [CodexError("not persisted yet"), OSError("cannot launch")]
)
def test_a_name_lookup_failure_still_delivers_the_hook(
    router: tuple[str, Received],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    error: CodexError | OSError,
) -> None:
    url, received = router
    payload = {**PAYLOAD, "hook_event_name": "Stop"}
    monkeypatch.setattr(sys, "stdin", StringIO(json.dumps(payload)))
    with patch.object(emit_module, "CodexClient", autospec=True) as factory:
        client = factory.return_value.__enter__.return_value
        client.thread_read.side_effect = error

        assert emit_module.main(url, SECRET) == 0

        factory.return_value.__exit__.assert_called_once()
    assert received.body == payload
    assert "session name lookup for s1 failed" in capsys.readouterr().err


@pytest.mark.parametrize("child", [False, True])
def test_codex_names_are_not_requested_for_claude_or_child_hooks(
    router: tuple[str, Received], monkeypatch: pytest.MonkeyPatch, child: bool
) -> None:
    url, received = router
    path = CODEX_HOOK_PATH if child else "/hooks/claude"
    payload = {**PAYLOAD, "hook_event_name": "Stop"}
    if child:
        payload["agent_id"] = "child"
    monkeypatch.setattr(sys, "stdin", StringIO(json.dumps(payload)))
    with patch.object(emit_module, "CodexClient", autospec=True) as factory:
        assert emit_module.main(base_of(url) + path, SECRET) == 0

        factory.assert_not_called()
    assert received.body == payload


def test_without_a_secret_nothing_is_posted(router: tuple[str, Received]) -> None:
    """Silently posting unauthenticated would just 401; saying so is what tells an
    operator their sessions are not being ingested."""
    url, received = router
    result = emit(["--path", CODEX_HOOK_PATH, "--url", url], {})

    assert result.returncode == 1
    assert received.body is None
    assert TOKEN_ENV in result.stderr


def test_an_unreachable_octomate_does_not_take_the_turn_down() -> None:
    """A session is the person's own work; ingest only observes it."""
    result = emit(
        ["--path", CODEX_HOOK_PATH, "--url", "http://127.0.0.1:1/hooks/codex"],
        {TOKEN_ENV: SECRET},
    )

    assert result.returncode == 1
    assert "failed" in result.stderr
    assert result.stdout.strip() == "{}"  # still no decision, rather than no answer


def test_the_target_resolves_from_the_environment_at_fire_time(
    router: tuple[str, Received],
) -> None:
    """The installed command carries only `--path`; the server's address comes from
    OCTOMATE_CLI_URL when the hook fires, so switching servers is an environment switch."""
    url, received = router
    result = emit(
        ["--path", CODEX_HOOK_PATH],
        {TOKEN_ENV: SECRET, OCTOMATE_URL_ENV: base_of(url)},
    )

    assert result.returncode == 0
    assert received.body == PAYLOAD


def test_a_pinned_url_wins_over_the_environment(router: tuple[str, Received]) -> None:
    """`--url` is the per-directory pin (a debug server's install); the environment
    must not silently redirect it."""
    url, received = router
    result = emit(
        ["--path", CODEX_HOOK_PATH, "--url", url],
        {TOKEN_ENV: SECRET, OCTOMATE_URL_ENV: "http://127.0.0.1:1"},
    )

    assert result.returncode == 0
    assert received.body == PAYLOAD


def test_without_a_target_nothing_is_posted_and_the_turn_survives() -> None:
    """No pin and no OCTOMATE_CLI_URL: say so on stderr and stay out of the way — a fresh
    machine without the environment set must not lose its session to ingest."""
    result = emit(["--path", CODEX_HOOK_PATH], {TOKEN_ENV: SECRET})

    assert result.returncode == 1
    assert OCTOMATE_URL_ENV in result.stderr
    assert result.stdout.strip() == "{}"  # the decision protocol still gets an answer


def test_the_claude_path_stays_silent_on_stdout(router: tuple[str, Received]) -> None:
    """A Claude `UserPromptSubmit` hook's stdout is injected into the turn's context,
    so on Claude's path the script must print nothing — the `{}` decision is Codex's
    protocol alone."""
    url, received = router
    result = emit(
        ["--path", "/hooks/claude"],
        {TOKEN_ENV: SECRET, OCTOMATE_URL_ENV: base_of(url)},
    )

    assert result.returncode == 0
    assert received.body == PAYLOAD
    assert result.stdout == ""


def test_the_config_file_backstops_a_bare_environment(
    router: tuple[str, Received], tmp_path: Path
) -> None:
    """A GUI-launched session never sourced a shell profile; the client config file is
    what keeps its hooks delivering with no environment at all."""
    url, received = router
    (tmp_path / ".config" / "octomate").mkdir(parents=True)
    (tmp_path / ".config" / "octomate" / "cli.toml").write_text(
        f'url = "{base_of(url)}"\ntoken = "{SECRET}"\n'
    )
    result = emit(["--path", CODEX_HOOK_PATH], {"HOME": str(tmp_path)})

    assert result.returncode == 0
    assert received.body == PAYLOAD
    assert received.authorization == f"Bearer {SECRET}"


def test_the_project_config_backstops_too_and_wins_over_the_user_scope(
    router: tuple[str, Received], tmp_path: Path
) -> None:
    """Hooks run with cwd at the session's directory; its `./.octomate/cli.toml` is
    that project's own override, resolved before the user file."""
    url, received = router
    (tmp_path / ".config" / "octomate").mkdir(parents=True)
    (tmp_path / ".config" / "octomate" / "cli.toml").write_text(
        'url = "http://127.0.0.1:1"\n'  # user scope points into the void
    )
    project = tmp_path / "project" / ".octomate"
    project.mkdir(parents=True)
    (project / "cli.toml").write_text(f'url = "{base_of(url)}"\ntoken = "{SECRET}"\n')
    result = subprocess.run(
        [sys.executable, str(EMIT_SCRIPT), "--path", CODEX_HOOK_PATH],
        input=json.dumps(PAYLOAD),
        capture_output=True,
        text=True,
        env={"PATH": "/usr/bin:/bin", "HOME": str(tmp_path)},
        cwd=str(tmp_path / "project"),
    )

    assert result.returncode == 0
    assert received.body == PAYLOAD


def test_the_duplicated_config_resolution_matches_the_canonical_one(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """emit.py mirrors the client config path with literals; behaviorally identical is
    what the mirror must stay."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.chdir(tmp_path)
    assert emit_module.config_files() == (project_config_path(), user_config_path())


def test_the_previous_generations_url_only_form_still_delivers(
    router: tuple[str, Received],
) -> None:
    """Hooks written before `--path` existed invoke `emit.py --url <url>`; they keep
    delivering until their next re-install, rather than breaking on upgrade."""
    url, received = router
    result = emit(["--url", url], {TOKEN_ENV: SECRET})

    assert result.returncode == 0
    assert received.body == PAYLOAD
    assert result.stdout.strip() == "{}"


def test_its_duplicated_names_still_match_the_canonical_ones() -> None:
    """emit.py repeats these as literals because it must not import the package (see
    below). Duplication across a boundary that cannot be crossed is the price; this is
    what stops it drifting — rename one and this fails rather than a session silently
    going unauthenticated or un-ingested.

    Held against `CLISettings` itself rather than a constant beside it, so renaming a
    *field* — which is what decides the variable — is caught here too.
    """
    assert TOKEN_ENV == CLISettings.env("token")
    assert OCTOMATE_URL_ENV == CLISettings.env("url")
    assert HOOK_TIMEOUT == CANONICAL_HOOK_TIMEOUT
    assert CODEX_HOOK_PATH == CANONICAL_CODEX_HOOK_PATH


def test_the_script_and_the_settings_class_agree_on_which_file_wins(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The other half of the duplication, and the half a rename would not catch: both
    must agree on which cli.toml wins. The script walks its files and takes the first
    hit, project scope first; the settings class hands pydantic one source whose
    *later* file wins, so it lists them the other way round. Two opposite spellings of
    one answer, and a reversal would quietly point a session at the wrong server.
    """
    monkeypatch.delenv(TOKEN_ENV, raising=False)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    project = tmp_path / "project"
    project.mkdir()
    monkeypatch.chdir(project)
    for path, value in (
        (user_config_path(), "from-the-user-file"),
        (project_config_path(), "from-the-project-file"),
    ):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f'token = "{value}"\n')

    from_the_script = emit_module.resolved(
        "token",
        TOKEN_ENV,
        [emit_module.file_config(path) for path in emit_module.config_files()],
    )

    assert from_the_script == "from-the-project-file"
    assert CLISettings().token == from_the_script


def test_the_script_never_imports_the_octomate_package() -> None:
    """Why this script exists at all: importing the package builds `Octomate` and pulls
    in pydantic-ai (~1.9s). Reading names needs the Codex SDK, but must not also
    import the server package into every blocking hook."""
    probe = (
        "import importlib.util, sys;"
        f"spec = importlib.util.spec_from_file_location('emit', {str(EMIT_SCRIPT)!r});"
        "module = importlib.util.module_from_spec(spec);"
        "spec.loader.exec_module(module);"
        "print(any(name == 'octomate' or name.startswith('octomate.') "
        "for name in sys.modules))"
    )
    result = subprocess.run(
        [sys.executable, "-c", probe], capture_output=True, text=True, check=True
    )
    assert result.stdout.strip() == "False"
