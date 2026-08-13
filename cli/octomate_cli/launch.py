"""Spawn the transcript tail for a native session — the launcher hook.

The forwarding hooks carry the ledger to Octomate over HTTP, but they can start
nothing on this machine; the transcript stream needs a local process, and this
command hook is what starts it. It fires on every prompt, spawns `octomate <agent>
tail` detached, and returns at once so the turn never waits — the tail itself refuses
to run twice per session, so the repeated fire is the liveness check, not a leak.
On a Codex `SubagentStop`, the event's `agent_transcript_path` rides along as
`--agent-path`: the running tail cannot see which sibling rollout is a child of its
session, so the hook that knows hands it the path (the tail spools it and exits when
one already holds the session).

The stream address is pinned only when the install pinned `--url`; otherwise it is
derived when the hook fires, from `OCTOMATE_URL` or the client config file — the same
resolution the forwarding hooks follow. With none of them, nothing spawns and nothing
is said: the emit hook on the same event already complained on stderr.

Run by absolute path, never as `python -m octomate...`, and imports nothing from
octomate: `octomate/__init__.py` builds `Octomate`, which costs ~1.9s to import, and
this command is on the blocking path of every prompt. The spawned tail pays that cost
detached, off it. Anything added here must keep the stdlib-only property; the
environment variable name and the client-config resolution are duplicated from
`octomate_cli/config.py` for the same reason — change them together.

Prints nothing on success: a `UserPromptSubmit` hook's stdout is injected into the
turn's context, so silence is the only correct answer.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tomllib
from pathlib import Path

OCTOMATE_URL_ENV = "OCTOMATE_URL"

USAGE = (
    "usage: launch.py [--url <stream-url>] [--path <hook-path>] "
    "[--agent <claude|codex>] --octomate <bin>"
)


def config_files() -> tuple[Path, Path]:
    """Project scope first, then user scope — mirroring `emit.py`'s resolution."""
    return (
        Path.cwd() / ".octomate" / "cli.toml",
        Path.home() / ".config" / "octomate" / "cli.toml",
    )


def configured_base() -> str | None:
    """The server base from the environment, then the config files in scope order. A
    file that does not parse counts as absent — the emit hook on the same event
    reports it."""
    value = os.environ.get(OCTOMATE_URL_ENV)
    if value:
        return value
    for path in config_files():
        try:
            table = tomllib.loads(path.read_text())
        except (OSError, tomllib.TOMLDecodeError):
            continue
        url = table.get("url")
        if isinstance(url, str) and url:
            return url
    return None


def stream_url(url: str | None, path: str | None) -> str | None:
    """The pinned stream URL, or one derived from the resolved base — the same
    `http(s) → ws(s)` + `/stream` derivation the installer's `stream_url_for` does,
    duplicated here because this script cannot import the package."""
    if url is not None:
        return url
    base = configured_base()
    if base is None or path is None:
        return None
    scheme, _, rest = base.rstrip("/").partition("://")
    return f"{'wss' if scheme == 'https' else 'ws'}://{rest}{path}/stream"


def main(url: str | None, path: str | None, agent: str, octomate_bin: str) -> int:
    event = json.load(sys.stdin)
    session_id = event.get("session_id")
    transcript_path = event.get("transcript_path")
    if not isinstance(session_id, str) or not isinstance(transcript_path, str):
        # An event that names no transcript has nothing to tail; the forwarding hooks
        # still carry the ledger, so this is not the place to complain.
        return 0
    target = stream_url(url, path)
    if target is None:
        return 0
    command = [
        octomate_bin,
        agent,
        "tail",
        "--session",
        session_id,
        "--path",
        transcript_path,
        "--url",
        target,
    ]
    cwd = event.get("cwd")
    if isinstance(cwd, str) and cwd:
        command += ["--cwd", cwd]
    agent_path = event.get("agent_transcript_path")
    if isinstance(agent_path, str) and agent_path:
        command += ["--agent-path", agent_path]
    subprocess.Popen(
        command,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        # Detached: the tail outlives this hook and never holds the turn.
        start_new_session=True,
    )
    return 0


if __name__ == "__main__":
    args = sys.argv[1:]
    url: str | None = None
    path: str | None = None
    agent = "claude"
    octomate_bin: str | None = None
    while args:
        flag = args.pop(0)
        if flag == "--url" and args:
            url = args.pop(0)
        elif flag == "--path" and args:
            path = args.pop(0)
        elif flag == "--agent" and args:
            agent = args.pop(0)
        elif flag == "--octomate" and args:
            octomate_bin = args.pop(0)
        else:
            print(USAGE, file=sys.stderr)
            raise SystemExit(2)
    if octomate_bin is None or (url is None and path is None):
        print(USAGE, file=sys.stderr)
        raise SystemExit(2)
    raise SystemExit(main(url, path, agent, octomate_bin))
