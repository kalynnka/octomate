"""Spawn the transcript tail for a native Claude session — the launcher hook.

The forwarding hooks carry the ledger to Octomate over HTTP, but they can start
nothing on this machine; the transcript stream needs a local process, and this
command hook is what starts it. It fires on every prompt, spawns `octomate claude
tail` detached, and returns at once so the turn never waits — the tail itself refuses
to run twice per session, so the repeated fire is the liveness check, not a leak.

The stream address is pinned only when the install pinned `--url`; otherwise it is
derived from `OCTOMATE_URL` when the hook fires, the same environment switch the
forwarding hooks follow. With neither, nothing spawns and nothing is said: the emit
hook on the same event already complained on stderr.

Run by absolute path, never as `python -m octomate...`, and imports nothing from
octomate: `octomate/__init__.py` builds `Octomate`, which costs ~1.9s to import, and
this command is on the blocking path of every prompt. The spawned tail pays that cost
detached, off it. Anything added here must keep the stdlib-only property; the
environment variable name is duplicated from `octomate_cli/hooks.py` for the same
reason — change both together.

Prints nothing on success: a `UserPromptSubmit` hook's stdout is injected into the
turn's context, so silence is the only correct answer.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys

OCTOMATE_URL_ENV = "OCTOMATE_URL"

USAGE = "usage: launch.py [--url <stream-url>] [--path <hook-path>] --octomate <bin>"


def stream_url(url: str | None, path: str | None) -> str | None:
    """The pinned stream URL, or one derived from the environment's base — the same
    `http(s) → ws(s)` + `/stream` derivation the installer's `stream_url_for` does,
    duplicated here because this script cannot import the package."""
    if url is not None:
        return url
    base = os.environ.get(OCTOMATE_URL_ENV, "").rstrip("/")
    if not base or path is None:
        return None
    scheme, _, rest = base.partition("://")
    return f"{'wss' if scheme == 'https' else 'ws'}://{rest}{path}/stream"


def main(url: str | None, path: str | None, octomate_bin: str) -> int:
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
        "claude",
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
    octomate_bin: str | None = None
    while args:
        flag = args.pop(0)
        if flag == "--url" and args:
            url = args.pop(0)
        elif flag == "--path" and args:
            path = args.pop(0)
        elif flag == "--octomate" and args:
            octomate_bin = args.pop(0)
        else:
            print(USAGE, file=sys.stderr)
            raise SystemExit(2)
    if octomate_bin is None or (url is None and path is None):
        print(USAGE, file=sys.stderr)
        raise SystemExit(2)
    raise SystemExit(main(url, path, octomate_bin))
