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
from threading import Thread

import pytest

from octomate.tentacles.agent.codex.emit import (
    DRIVEN_ENV,
    HOOK_SECRET_ENV,
    HOOK_TIMEOUT,
)
from octomate.tentacles.agent.codex.hooks import DRIVEN_ENV as CANONICAL_DRIVEN_ENV
from octomate.tentacles.agent.codex.hooks import HOOK_TIMEOUT as CANONICAL_HOOK_TIMEOUT
from octomate.tentacles.agent.codex.typer import EMIT_SCRIPT
from octomate.tentacles.agent.typer import HOOK_SECRET_ENV as CANONICAL_HOOK_SECRET_ENV

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
    url: str, env: dict[str, str], payload: dict[str, object] | None = None
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(EMIT_SCRIPT), "--url", url],
        input=json.dumps(payload if payload is not None else PAYLOAD),
        capture_output=True,
        text=True,
        env={"PATH": "/usr/bin:/bin", **env},
    )


def test_the_payload_is_delivered_bearing_the_hook_credential(
    router: tuple[str, Received],
) -> None:
    url, received = router
    result = emit(url, {HOOK_SECRET_ENV: SECRET})

    assert result.returncode == 0
    assert received.body == PAYLOAD
    assert received.authorization == f"Bearer {SECRET}"
    # Codex reads stdout as the hook's decision; an observer decides nothing.
    assert result.stdout.strip() == "{}"


def test_a_driven_session_is_marked_so_the_router_can_drop_it(
    router: tuple[str, Received],
) -> None:
    url, received = router
    emit(url, {HOOK_SECRET_ENV: SECRET, DRIVEN_ENV: "1"})

    assert received.body is not None
    assert received.body["octomate_driven"] is True


def test_an_undriven_session_is_not_marked(router: tuple[str, Received]) -> None:
    url, received = router
    emit(url, {HOOK_SECRET_ENV: SECRET})

    assert received.body is not None
    assert "octomate_driven" not in received.body


def test_without_a_secret_nothing_is_posted(router: tuple[str, Received]) -> None:
    """Silently posting unauthenticated would just 401; saying so is what tells an
    operator their sessions are not being ingested."""
    url, received = router
    result = emit(url, {})

    assert result.returncode == 1
    assert received.body is None
    assert HOOK_SECRET_ENV in result.stderr


def test_an_unreachable_octomate_does_not_take_the_turn_down() -> None:
    """A session is the person's own work; ingest only observes it."""
    result = emit("http://127.0.0.1:1/hooks/codex", {HOOK_SECRET_ENV: SECRET})

    assert result.returncode == 1
    assert "failed" in result.stderr
    assert result.stdout.strip() == "{}"  # still no decision, rather than no answer


def test_its_duplicated_names_still_match_the_canonical_ones() -> None:
    """emit.py repeats these as literals because it must not import the package (see
    below). Duplication across a boundary that cannot be crossed is the price; this is
    what stops it drifting — rename one and this fails rather than a session silently
    going unauthenticated or un-ingested."""
    assert HOOK_SECRET_ENV == CANONICAL_HOOK_SECRET_ENV
    assert DRIVEN_ENV == CANONICAL_DRIVEN_ENV
    assert HOOK_TIMEOUT == CANONICAL_HOOK_TIMEOUT


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
