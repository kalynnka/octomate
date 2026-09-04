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
from pathlib import Path
from threading import Thread

import pytest
from octomate_cli import emit as emit_module
from octomate_cli.codex import CODEX_HOOK_PATH as CANONICAL_CODEX_HOOK_PATH
from octomate_cli.codex import EMIT_SCRIPT
from octomate_cli.codex import HOOK_TIMEOUT as CANONICAL_HOOK_TIMEOUT
from octomate_cli.config import CLISettings, project_config_path, user_config_path
from octomate_cli.emit import (
    CODEX_HOOK_PATH,
    DRIVEN_ENV,
    HOOK_TIMEOUT,
    OCTOMATE_URL_ENV,
    SECRET_ENV,
)

from octomate.tentacles.codex.hooks import DRIVEN_ENV as CANONICAL_DRIVEN_ENV

SECRET = "the-hook-secret"
PAYLOAD = {"hook_event_name": "Stop", "session_id": "s1", "turn_id": "t1"}


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


def emit(
    args: list[str], env: dict[str, str], payload: dict[str, object] | None = None
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
    result = emit(["--path", CODEX_HOOK_PATH, "--url", url], {SECRET_ENV: SECRET})

    assert result.returncode == 0
    assert received.body == PAYLOAD
    assert received.authorization == f"Bearer {SECRET}"
    # Codex reads stdout as the hook's decision; an observer decides nothing.
    assert result.stdout.strip() == "{}"


def test_a_driven_session_is_marked_so_the_router_can_drop_it(
    router: tuple[str, Received],
) -> None:
    url, received = router
    emit(
        ["--path", CODEX_HOOK_PATH, "--url", url],
        {SECRET_ENV: SECRET, DRIVEN_ENV: "1"},
    )

    assert received.body is not None
    assert received.body["octomate_driven"] is True


def test_an_undriven_session_is_not_marked(router: tuple[str, Received]) -> None:
    url, received = router
    emit(["--path", CODEX_HOOK_PATH, "--url", url], {SECRET_ENV: SECRET})

    assert received.body is not None
    assert "octomate_driven" not in received.body


def test_without_a_secret_nothing_is_posted(router: tuple[str, Received]) -> None:
    """Silently posting unauthenticated would just 401; saying so is what tells an
    operator their sessions are not being ingested."""
    url, received = router
    result = emit(["--path", CODEX_HOOK_PATH, "--url", url], {})

    assert result.returncode == 1
    assert received.body is None
    assert SECRET_ENV in result.stderr


def test_an_unreachable_octomate_does_not_take_the_turn_down() -> None:
    """A session is the person's own work; ingest only observes it."""
    result = emit(
        ["--path", CODEX_HOOK_PATH, "--url", "http://127.0.0.1:1/hooks/codex"],
        {SECRET_ENV: SECRET},
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
        {SECRET_ENV: SECRET, OCTOMATE_URL_ENV: base_of(url)},
    )

    assert result.returncode == 0
    assert received.body == PAYLOAD


def test_a_pinned_url_wins_over_the_environment(router: tuple[str, Received]) -> None:
    """`--url` is the per-directory pin (a debug server's install); the environment
    must not silently redirect it."""
    url, received = router
    result = emit(
        ["--path", CODEX_HOOK_PATH, "--url", url],
        {SECRET_ENV: SECRET, OCTOMATE_URL_ENV: "http://127.0.0.1:1"},
    )

    assert result.returncode == 0
    assert received.body == PAYLOAD


def test_without_a_target_nothing_is_posted_and_the_turn_survives() -> None:
    """No pin and no OCTOMATE_CLI_URL: say so on stderr and stay out of the way — a fresh
    machine without the environment set must not lose its session to ingest."""
    result = emit(["--path", CODEX_HOOK_PATH], {SECRET_ENV: SECRET})

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
        {SECRET_ENV: SECRET, OCTOMATE_URL_ENV: base_of(url)},
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
        f'url = "{base_of(url)}"\nsecret = "{SECRET}"\n'
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
    (project / "cli.toml").write_text(f'url = "{base_of(url)}"\nsecret = "{SECRET}"\n')
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
    result = emit(["--url", url], {SECRET_ENV: SECRET})

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
    assert SECRET_ENV == CLISettings.env("secret")
    assert OCTOMATE_URL_ENV == CLISettings.env("url")
    assert DRIVEN_ENV == CANONICAL_DRIVEN_ENV
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
    monkeypatch.delenv(SECRET_ENV, raising=False)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    project = tmp_path / "project"
    project.mkdir()
    monkeypatch.chdir(project)
    for path, value in (
        (user_config_path(), "from-the-user-file"),
        (project_config_path(), "from-the-project-file"),
    ):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f'secret = "{value}"\n')

    from_the_script = emit_module.resolved(
        "secret",
        SECRET_ENV,
        [emit_module.file_config(path) for path in emit_module.config_files()],
    )

    assert from_the_script == "from-the-project-file"
    assert CLISettings().secret == from_the_script


def test_the_script_never_imports_the_octomate_package() -> None:
    """Why this script exists at all: importing the package builds `Octomate` and pulls
    in pydantic-ai (~1.9s), and Codex blocks on this hook twice a turn. Run by path with
    stdlib imports only, it stays ~50ms. An `octomate` import here would silently hand
    every turn that cost back."""
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
