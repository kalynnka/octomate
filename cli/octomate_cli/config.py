"""The client's own configuration — the two facts the CLI needs to reach Octomate:
where the server is, and the credential its hook routers require. Owned here so a
machine holding only octomate-cli never touches the server's config.

Resolution is the same everywhere, per key, following Claude's own settings pattern:
an explicit flag wins, then the environment, then the project's `./.octomate/cli.toml`
(hooks run with cwd at the directory a session lives in), then the user's
`~/.config/octomate/cli.toml`. The files are what survive launch paths the
environment does not — a GUI-launched editor never sourced a shell profile — and the
project scope is how one directory aims at a different server (a debug instance)
without touching the machine's default. The stdlib hook scripts (`emit.py`,
`launch.py`) mirror this resolution with duplicated literals, since they cannot
import the package; the tests hold the copies together.
"""

from __future__ import annotations

import json
import os
import secrets
import tomllib
from enum import Enum
from pathlib import Path
from typing import Annotated

import typer

# How a client carries the credential. The server's config reads the same variable,
# so one export serves both halves on the machine that runs them both.
HOOK_SECRET_ENV = "OCTOMATE__HOOK_SECRET"

# Where Octomate is, as a base URL (`http://host:port`) the hook scripts resolve when
# a hook fires — so switching servers is an environment switch, not a re-install. An
# explicit `--url` pinned at install time wins over it.
OCTOMATE_URL_ENV = "OCTOMATE_URL"


class Scope(str, Enum):
    user = "user"
    project = "project"


def project_config_path() -> Path:
    """`./.octomate/cli.toml` — beside the directory a session runs in, the way
    `.claude/settings.json` scopes Claude. On a server machine `.octomate/` is also
    the data directory; the two uses share the name, not their files."""
    return Path.cwd() / ".octomate" / "cli.toml"


def user_config_path() -> Path:
    """`~/.config/octomate/cli.toml` — where CLI tools keep their config, git and
    ruff alike."""
    return Path.home() / ".config" / "octomate" / "cli.toml"


def load_config(path: Path) -> dict[str, object]:
    """One file's table, `{}` when absent. A file that does not parse is an
    operator's mistake worth a precise error, not a silent fallback."""
    try:
        text = path.read_text()
    except OSError:
        return {}
    try:
        return tomllib.loads(text)
    except tomllib.TOMLDecodeError as error:
        raise typer.BadParameter(f"{path}: {error}") from None


def resolved(key: str, env: str) -> str | None:
    """One value by the client's precedence: the environment, then the project file,
    then the user file."""
    value = os.environ.get(env)
    if value:
        return value
    for path in (project_config_path(), user_config_path()):
        from_file = load_config(path).get(key)
        if isinstance(from_file, str) and from_file:
            return from_file
    return None


def resolved_url() -> str | None:
    return resolved("url", OCTOMATE_URL_ENV)


def resolved_secret() -> str | None:
    return resolved("hook_secret", HOOK_SECRET_ENV)


def configure(
    url: Annotated[
        str | None,
        typer.Option(help="Octomate's base URL (http://host:port)."),
    ] = None,
    secret: Annotated[
        str | None,
        typer.Option(
            help="Hook credential. Omitted, the one already resolving is kept, and "
            "one is generated when nothing resolves anywhere."
        ),
    ] = None,
    scope: Annotated[
        Scope,
        typer.Option(
            help="Which file to write: 'user' (~/.config/octomate/cli.toml) or "
            "'project' (./.octomate/cli.toml, resolved first)."
        ),
    ] = Scope.user,
) -> None:
    """Write a client config file the hooks and the tail fall back to.

    The environment still overrides both scopes, and a `--url` pinned at install time
    beats everything — the files are the durable floor, so ingest works from any
    launch path on a machine where octomate-cli is the only thing installed.
    """
    path = project_config_path() if scope is Scope.project else user_config_path()
    current = load_config(path)
    if url is not None:
        current["url"] = url
    generated = False
    if secret is None:
        secret = resolved_secret()
        if secret is None:
            secret = secrets.token_urlsafe(32)
            generated = True
    current["hook_secret"] = secret

    # json.dumps output is a valid TOML basic string: the escapes JSON emits are the
    # subset TOML shares, so no hand-rolled quoting and no extra dependency.
    content = "".join(
        f"{key} = {json.dumps(value)}\n"
        for key, value in current.items()
        if isinstance(value, str)
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    path.chmod(0o600)  # it holds a credential

    typer.echo(f"Wrote {path}")
    typer.echo(
        f"  url:    {current.get('url', f'(unset — hooks need ${OCTOMATE_URL_ENV})')}"
    )
    typer.echo(f"  secret: {'generated' if generated else 'kept'}")
    if generated:
        typer.secho(
            f"\nThe server must hold the same credential — its octomate.yaml "
            f"`hook_secret`, or {HOOK_SECRET_ENV} in its environment:\n"
            f"  {secret}",
            fg=typer.colors.YELLOW,
            err=True,
        )
