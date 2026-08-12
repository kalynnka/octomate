"""Spawn the transcript tail for a native Claude session — the launcher hook.

Claude Code reaches Octomate's hook router over native `http` handlers, which can
start nothing on this machine; the transcript stream needs a local process, and this
command hook is what starts it. It fires on every prompt, spawns `octomate claude
tail` detached, and returns at once so the turn never waits — the tail itself refuses
to run twice per session, so the repeated fire is the liveness check, not a leak.

Run by absolute path, never as `python -m octomate...`, and imports nothing from
octomate: `octomate/__init__.py` builds `Octomate`, which costs ~1.9s to import, and
this command is on the blocking path of every prompt. The spawned tail pays that cost
detached, off it. Anything added here must keep the stdlib-only property.

Prints nothing on success: a `UserPromptSubmit` hook's stdout is injected into the
turn's context, so silence is the only correct answer.
"""

from __future__ import annotations

import json
import subprocess
import sys


def main(url: str, octomate_bin: str) -> int:
    event = json.load(sys.stdin)
    session_id = event.get("session_id")
    transcript_path = event.get("transcript_path")
    if not isinstance(session_id, str) or not isinstance(transcript_path, str):
        # An event that names no transcript has nothing to tail; the http hooks still
        # carry the ledger, so this is not the place to complain.
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
        url,
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
    if len(sys.argv) != 5 or sys.argv[1] != "--url" or sys.argv[3] != "--octomate":
        print(
            "usage: launch.py --url <stream-url> --octomate <octomate-bin>",
            file=sys.stderr,
        )
        raise SystemExit(2)
    raise SystemExit(main(sys.argv[2], sys.argv[4]))
