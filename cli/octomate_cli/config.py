"""The client's own configuration — the two facts the CLI needs to reach Octomate:
where the server is, and the credential its hook routers require. A settings class,
the way the deployment's own config is one, so the variables, the files and their
precedence are declared in `CLISettings` rather than assembled by hand, and every
command reads the one `cli_settings()` object rather than building its own. Owned here
so a machine holding only octomate-cli never touches the server's config.

The stdlib hook scripts (`emit.py`, `launch.py`) cannot use it, and that is not an
oversight to be tidied away later. They exist to stay off the import cost of a
package on a hook the session blocks on, several times a turn: `tomllib` costs ~7ms
against `pydantic_settings`' ~230ms, on a script whose whole budget is ~25ms. So they
re-spell the variable names and the same file precedence in stdlib. Two tests in
`tests/agent/test_codex_emit.py` are what stop them drifting — one holds the names
against `CLISettings.env`, the other that both halves still pick the same file — so
a renamed field fails a test rather than a session going unauthenticated.
"""

from __future__ import annotations

import json
import secrets
import tomllib
from enum import Enum
from functools import cache
from pathlib import Path
from typing import Annotated

import typer
from pydantic import Field
from pydantic_settings import (
    BaseSettings,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
    TomlConfigSettingsSource,
)
from rich.console import Console
from rich.panel import Panel

# Guidance goes to stderr, so a caller reading this command's stdout gets the file
# report alone.
console = Console(stderr=True)


class Scope(str, Enum):
    user = "user"
    project = "project"


# The two files, as `toml_file` wants them and as the source of both spellings below.
# Left unexpanded on purpose: pydantic resolves `~` and the relative path per settings
# object rather than at import, which is what lets one process read the cli.toml of
# whatever directory it was started in — a hook's cwd is its session's.
USER_CONFIG = "~/.config/octomate/cli.toml"
PROJECT_CONFIG = ".octomate/cli.toml"


def user_config_path() -> Path:
    """`~/.config/octomate/cli.toml` — where CLI tools keep their config, git and
    ruff alike."""
    return Path(USER_CONFIG).expanduser()


def project_config_path() -> Path:
    """`./.octomate/cli.toml` — beside the directory a session runs in, the way
    `.claude/settings.json` scopes Claude. On a server machine `.octomate/` is also
    the data directory; the two uses share the name, not their files."""
    return Path.cwd() / PROJECT_CONFIG


class CLISettings(BaseSettings):
    """What this machine's client half knows, resolved the same way everywhere.

    Reached through `cli_settings()` rather than constructed: one object per process,
    which is what a command or a hook is.
    """

    model_config = SettingsConfigDict(
        # The reason there is a prefix: the server's settings read `OCTOMATE__` with
        # `__` between the levels, so a client variable under that prefix would read
        # as a deployment key — and one of them, the retired deployment secret,
        # literally was.
        env_prefix="OCTOMATE_CLI_",
        # An exported-but-empty variable means unset, and the files under it still
        # answer — what a shell that cleared one intends.
        env_ignore_empty=True,
        # Weakest first: within one TOML source the later file wins, so the project's
        # own beats the machine's. That is how one directory aims at a different
        # server (a debug instance) without touching the default.
        toml_file=(USER_CONFIG, PROJECT_CONFIG),
    )

    url: str | None = Field(
        default=None,
        description=(
            "Octomate's base URL (`http://host:port`), resolved when a hook fires "
            "rather than pinned at install time — so switching servers is a config "
            "switch, not a re-install. An explicit `--url` beats it."
        ),
    )
    secret: str | None = Field(
        default=None,
        description=(
            "This person's own bearer, which the hook routers and the served MCP "
            "endpoints authenticate. Minted by `octomate configure` and registered "
            "by an admin under `users.<name>.secret`."
        ),
    )

    @classmethod
    def env(cls, field: str) -> str:
        """`CLISettings.env("secret")` — the variable that field reads.

        The class is asked every time rather than a constant standing beside it, so
        no variable name is ever spelled by hand: this composes from the prefix
        pydantic is holding, and a renamed field changes what every message says.

        A method and not `__class_getitem__`, which would read better at the ~20 call
        sites: a subscript on a class is a *type* to a type checker, so pyright reads
        `CLISettings["secret"]` as specialising the model and calls the result
        `type[CLISettings]` whatever the override returns. Every site would be
        mistyped, and silently so wherever the value is only interpolated.

        `.get` with a default rather than a subscript because the key is optional in
        `SettingsConfigDict`, and an absent prefix is the empty one.
        """
        return f"{cls.model_config.get('env_prefix', '')}{field}".upper()

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        """Which kinds of source there are, and in what order — per key, following
        Claude's own settings pattern: an explicit value wins, then the environment,
        then the files. Which files is `toml_file`'s to say, above.

        The environment is ahead of the files for the cases with no home to write
        into, a container or a CI step. Everywhere else the files are the durable
        home and what `configure` writes: they survive launch paths the environment
        does not — a GUI-launched editor never sourced a shell profile — and they are
        per-user, where an exported credential is inherited by everything that shell
        starts.

        No dotenv and no secrets dir: `./.env` on a server machine is the
        *deployment's* environment, and reading it here would let a server's file
        decide a person's credential.
        """
        return (init_settings, env_settings, TomlConfigSettingsSource(settings_cls))


@cache
def cli_settings() -> CLISettings:
    """This process's client config — the one object every command reads.

    Typer has nothing like FastAPI's `Depends` to hand a command its config, and its
    one shared-state channel, `ctx.obj`, is untyped and reaches only commands invoked
    through the root app. So the dependency is this function, and every call site
    asks it rather than building a settings object of its own.

    Cached because a process is one CLI invocation or one hook, and neither moves its
    own environment or working directory while it runs. Not for speed: a settings
    object costs ~0.13ms to build and no invocation wants more than two. The suite is
    the one process that plays many, and `tests/conftest.py` clears this between them.
    """
    return CLISettings()


def load_config(path: Path) -> dict[str, object]:
    """One file's table, `{}` when absent — what `configure` reads back before
    rewriting it, so keys it does not own survive. A file that does not parse is an
    operator's mistake worth a precise error, not a silent fallback."""
    try:
        text = path.read_text()
    except OSError:
        return {}
    try:
        return tomllib.loads(text)
    except tomllib.TOMLDecodeError as error:
        raise typer.BadParameter(f"{path}: {error}") from None


def configure(
    url: Annotated[
        str | None,
        typer.Option(help="Octomate's base URL (http://host:port)."),
    ] = None,
    secret: Annotated[
        str | None,
        typer.Option(
            help="Your own credential. Omitted, the one already resolving is kept, "
            "and one is generated when nothing resolves anywhere."
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
    """Write the client config every hook, tail and MCP entry on this machine reads.

    The first of the three steps that set a person up: this mints the credential and
    puts it somewhere durable, the panel it prints says how to get it registered, and
    the runtimes' own `hooks install` / `mcp install` come last, once there is
    something for them to resolve.

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
        secret = cli_settings().secret
        if secret is None:
            secret = secrets.token_urlsafe(32)
            generated = True
    current["secret"] = secret

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
    # The one command that moves what `cli_settings()` answers, so it is the one that
    # has to drop the cached answer: anything reading after this wants the file just
    # written, not the object built before it existed.
    cli_settings.cache_clear()

    address = current.get("url") or f"(unset — hooks need ${CLISettings.env('url')})"
    typer.echo(f"Wrote {path}")
    typer.echo(f"  url:    {address}")
    typer.echo(f"  secret: {'generated' if generated else 'kept'}")
    # Prose is left for rich to wrap; lines meant to be copied are kept short enough
    # to survive intact, since a token split across two lines is worse than no help.
    steps = [
        "Point your runtimes at it. Each writes down what it resolves here, so "
        "these come after the credential exists — and again if it moves:",
        "",
        "  [cyan]octomate claude hooks install[/]   [cyan]octomate claude mcp install[/]",
        "  [cyan]octomate codex hooks install[/]    [cyan]octomate codex mcp install[/]",
    ]
    if not generated:
        console.print(Panel("\n".join(steps), title="[bold]next[/]", padding=(1, 2)))
        return
    # A generated token is url-safe and a supplied one never reaches here, so no
    # value below can carry markup.
    console.print(
        Panel(
            "\n".join(
                [
                    "[yellow]Octomate cannot see this credential yet[/], and its "
                    "routers refuse a bearer they do not know. Give it to the "
                    "server's admin to add as yours:",
                    "",
                    "  [dim]users:[/]",
                    "    [dim]<your username>:[/]",
                    f"      [dim]secret:[/] [green]{secret}[/]",
                    "",
                    *steps,
                ]
            ),
            title="[bold yellow]register this credential[/]",
            border_style="yellow",
            padding=(1, 2),
        )
    )
