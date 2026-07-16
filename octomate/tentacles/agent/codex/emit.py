"""Forward a Codex hook payload from stdin to Octomate's hook router.

Codex has no `http` hook handler — its handlers are `command`, `prompt` or `agent` — so
a native session reaches Octomate through a command, and this is that command. The
transport is still HTTP: this only carries stdin to the router.

Run by absolute path, never as `python -m octomate...`, and imports nothing from
octomate. Both matter: `octomate/__init__.py` builds `Octomate`, which pulls in
pydantic-ai and costs ~1.9s to import — per hook, on a handler Codex blocks on, twice a
turn. By path with stdlib only it is ~50ms, because Python never imports the package.

Anything added here must keep that property: stdlib imports only. The two environment
variable names below are duplicated from `octomate/tentacles/agent/typer.py` and
`octomate/tentacles/agent/codex/hooks.py` for the same reason — this module cannot
import them without paying for the whole package. Change them together.
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request

HOOK_SECRET_ENV = "OCTOMATE__HOOK_SECRET"
DRIVEN_ENV = "OCTOMATE_CODEX_DRIVEN"
HOOK_TIMEOUT = 10


def main(url: str) -> int:
    payload = json.load(sys.stdin)
    if os.environ.get(DRIVEN_ENV) == "1":
        # Octomate is driving this session itself and already records the run; the
        # router drops the event rather than writing the conversation a second time.
        payload["octomate_driven"] = True

    secret = os.environ.get(HOOK_SECRET_ENV)
    if not secret:
        print(
            f"octomate: {HOOK_SECRET_ENV} is unset — this session is not being "
            "ingested. Export it to match Octomate's hook_secret.",
            file=sys.stderr,
        )
        return 1

    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode(),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {secret}",
        },
    )
    try:
        urllib.request.urlopen(request, timeout=HOOK_TIMEOUT).close()
    except (urllib.error.URLError, OSError) as error:
        # Report and get out of the way. A session is a person's own work, and Octomate
        # only observes it; an ingest that cannot deliver must not take the turn down
        # with it.
        print(f"octomate: hook delivery to {url} failed: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    if len(sys.argv) != 3 or sys.argv[1] != "--url":
        print("usage: emit.py --url <hook-url>", file=sys.stderr)
        raise SystemExit(2)
    status = main(sys.argv[2])
    # Codex reads stdout as the hook's decision; an empty object decides nothing, which
    # is what an observer should do.
    print("{}")
    raise SystemExit(status)
