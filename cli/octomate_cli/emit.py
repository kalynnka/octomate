"""Forward a native session's hook payload from stdin to Octomate's hook router.

Both agents' installers write `command` hooks that run this script — Codex because it
has no `http` hook handler at all, Claude so the settings file stays free of hosts and
credentials: with no `--url` pinned, the router's address is resolved when the hook
fires, from `OCTOMATE_CLI_URL` or the client config file, and switching servers is an
environment switch, not a re-install. The transport is HTTP either way: this only
carries stdin to the router at `<base>{--path}`.

Run by absolute path, never as `python -m octomate...`, and imports nothing from
octomate. Both matter: `octomate/__init__.py` builds `Octomate`, which pulls in
pydantic-ai and costs ~1.9s to import — per hook, on a handler the session blocks on,
several times a turn. By path with stdlib only it is ~25ms, because Python never
imports the package.

Anything added here must keep that property: stdlib imports only. The environment
variable names, the hook path, and the client-config resolution below are duplicated
from `octomate_cli/config.py` and `octomate_cli/codex.py` (and octomate's
`tentacles/codex/hooks.py` holds `DRIVEN_ENV`) for the same reason — this
module cannot import them without paying for a package. Change them together; the
tests hold the copies to the canonical ones.
"""

from __future__ import annotations

import json
import os
import sys
import tomllib
import urllib.error
import urllib.request
from pathlib import Path

SECRET_ENV = "OCTOMATE_CLI_SECRET"
OCTOMATE_URL_ENV = "OCTOMATE_CLI_URL"
DRIVEN_ENV = "OCTOMATE_CODEX_DRIVEN"
CODEX_HOOK_PATH = "/hooks/codex"
HOOK_TIMEOUT = 10

USAGE = "usage: emit.py --path <hook-path> [--url <hook-url>]"


def config_files() -> tuple[Path, Path]:
    """Project scope first, then user scope — hooks run with cwd at the directory the
    session lives in, so `./.octomate/cli.toml` is that project's own override."""
    return (
        Path.cwd() / ".octomate" / "cli.toml",
        Path.home() / ".config" / "octomate" / "cli.toml",
    )


def file_config(path: Path) -> dict[str, object]:
    """One file's table, `{}` when absent. A file that does not parse is reported and
    treated as absent: this runs on the blocking path of someone's session, where a
    traceback helps nobody."""
    try:
        text = path.read_text()
    except OSError:
        return {}
    try:
        return tomllib.loads(text)
    except tomllib.TOMLDecodeError as error:
        print(f"octomate: ignoring unparseable {path}: {error}", file=sys.stderr)
        return {}


def resolved(key: str, env: str, tables: list[dict[str, object]]) -> str | None:
    """The client's precedence: the environment, then the config files in scope
    order."""
    value = os.environ.get(env)
    if value:
        return value
    for table in tables:
        from_file = table.get(key)
        if isinstance(from_file, str) and from_file:
            return from_file
    return None


def main(url: str, secret: str | None) -> int:
    payload = json.load(sys.stdin)
    if os.environ.get(DRIVEN_ENV) == "1":
        # Octomate is driving this session itself and already records the run; the
        # router drops the event rather than writing the conversation a second time.
        payload["octomate_driven"] = True

    if not secret:
        print(
            f"octomate: no credential — {SECRET_ENV} is unset and the "
            "client config holds none, so this session is not being ingested. "
            "Run `octomate configure`.",
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
    args = sys.argv[1:]
    path: str | None = None
    url: str | None = None
    while args:
        flag = args.pop(0)
        if flag == "--path" and args:
            path = args.pop(0)
        elif flag == "--url" and args:
            url = args.pop(0)
        else:
            print(USAGE, file=sys.stderr)
            raise SystemExit(2)
    # `--url` alone is the previous generation's installed form; honoring it keeps
    # hooks written before `--path` existed delivering until their next re-install.
    if path is None and url is None:
        print(USAGE, file=sys.stderr)
        raise SystemExit(2)
    tables = [file_config(candidate) for candidate in config_files()]
    if url is None:
        base = resolved("url", OCTOMATE_URL_ENV, tables)
        url = base.rstrip("/") + str(path) if base else None
    if url is None:
        print(
            f"octomate: no target — {OCTOMATE_URL_ENV} is unset, the client config "
            "names no url, and no --url was pinned. This session is not being "
            "ingested; run `octomate configure`.",
            file=sys.stderr,
        )
        status = 1
    else:
        status = main(url, resolved("secret", SECRET_ENV, tables))
    if path is None or path == CODEX_HOOK_PATH:
        # Codex reads stdout as the hook's decision; an empty object decides nothing,
        # which is what an observer should do. A Claude hook's stdout is injected into
        # the turn's context instead, so on Claude's path silence is the only correct
        # answer.
        print("{}")
    raise SystemExit(status)
