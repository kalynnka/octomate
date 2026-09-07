"""Install and manage the claude tentacle's native hooks."""

from __future__ import annotations

import shlex
import sys
from enum import Enum
from pathlib import Path
from typing import Annotated, Literal

import typer

from octomate_cli.config import CLISettings
from octomate_cli.tentacles.claude.config import load_settings, write_settings
from octomate_cli.tentacles.hooks import EMIT_SCRIPT, LAUNCH_SCRIPT, announce_secret
from octomate_cli.tentacles.types import JsonObject, JsonValue

# The events the hook pipe registers and the server acts on. `UserPromptSubmit` and
# `Stop` carry the turn's prompt and answer — the whole human ledger — while
# `SessionEnd` closes the session so the transcript tailer can finalize.
# `SubagentStart`/`SubagentStop` bound a subagent's life the same way one level down.
#
# `SessionStart` stays absent even though a `command` hook could receive it: the
# server acts on nothing in it — the first prompt starts the session — so registering
# it would spawn a process per session for nothing.
HandledHookEvent = Literal[
    "UserPromptSubmit",
    "Stop",
    "SessionEnd",
    "SubagentStart",
    "SubagentStop",
]


HANDLED_HOOK_EVENTS: tuple[HandledHookEvent, ...] = (
    "UserPromptSubmit",
    "Stop",
    "SessionEnd",
    "SubagentStart",
    "SubagentStop",
)


# Bound so a wedged or slow Octomate can never freeze someone's Claude session: past
# this the CLI abandons the hook and carries on.
HOOK_TIMEOUT = 10


# The hook route's path as clients address it: settings point at
# `http://<host>:<port>{CLAUDE_HOOK_PATH}`. The server inlines the literal in its
# route; the tests that speak to it are what keep the two matching.
CLAUDE_HOOK_PATH = "/hooks/claude"


# The transcript stream's path, likewise: a tail connects to
# `ws://<host>:<port>{CLAUDE_STREAM_PATH}` bearing the same hook credential.
CLAUDE_STREAM_PATH = "/hooks/claude/stream"


hooks_typer = typer.Typer(
    help="Manage the Claude Code hook pipe into Octomate.", no_args_is_help=True
)


# Preserve the existing enum stringification used by CLI option defaults.
class Scope(str, Enum):  # noqa: UP042
    user = "user"
    project = "project"


ScopeOption = Annotated[
    Scope,
    typer.Option(
        help="Which settings file to touch: 'user' (~/.claude) or 'project' (./.claude)."
    ),
]


SettingsOption = Annotated[
    Path | None,
    typer.Option(help="Explicit settings.json path; overrides --scope when given."),
]


def settings_file(scope: Scope, settings: Path | None) -> Path:
    if settings is not None:
        return settings
    root = Path.home() if scope is Scope.user else Path.cwd()
    return root / ".claude" / "settings.json"


def claude_emit_handler(url: str | None) -> JsonObject:
    """One forwarding `command` hook: `emit.py` carries the event body from stdin to
    the hook router, reading the credential — and, unless `url` pins one, the router's
    address (`OCTOMATE_CLI_URL`) — from the environment at fire time. A command rather
    than a native `http` handler so the settings file stays free of hosts and
    credentials both: the same install serves whichever server the environment names.
    Synchronous either way, which is what guarantees delivery before a short-lived
    `claude -p` exits."""
    command = [sys.executable, str(EMIT_SCRIPT), "--path", CLAUDE_HOOK_PATH]
    if url is not None:
        command += ["--url", url]
    return {
        "type": "command",
        "command": shlex.join(command),
        "timeout": HOOK_TIMEOUT,
    }


def stream_url_for(hook_url: str) -> str:
    """The stream endpoint the hook URL implies: same host, ws(s) for http(s)."""
    base = hook_url.removesuffix(CLAUDE_HOOK_PATH)
    scheme, _, rest = base.partition("://")
    return f"{'wss' if scheme == 'https' else 'ws'}://{rest}{CLAUDE_STREAM_PATH}"


def claude_launch_handler(hook_url: str | None) -> JsonObject:
    """The launcher `command` hook: spawns `octomate claude tail` for the session,
    detached (`launch.py`) — the forwarding hooks reach Octomate but can start nothing
    on this machine, and the stream needs a local process. The command pins this
    installer's own interpreter and octomate script by absolute path, so it works from
    whatever shell Claude runs hooks in; the stream address is pinned only when the
    install pinned `--url`, and otherwise resolved from `OCTOMATE_CLI_URL` at fire time,
    like the credential always is."""
    command = [
        sys.executable,
        str(LAUNCH_SCRIPT),
        "--path",
        CLAUDE_HOOK_PATH,
        "--octomate",
        str(Path(sys.argv[0]).resolve()),
    ]
    if hook_url is not None:
        command += ["--url", stream_url_for(hook_url)]
    return {"type": "command", "command": shlex.join(command), "timeout": HOOK_TIMEOUT}


def is_octomate_hook(hook: JsonValue) -> bool:
    """A handler this installer wrote: a `command` carrying Octomate's hook path
    (which the stream path extends, so pinned launchers of every age match too), or
    the `http` handler an older install pointed at it. Matched by path, not the exact
    command, so a re-install replaces a stale handler whatever its host, port,
    interpreter, or script location — every generation back to the http ones."""
    if not isinstance(hook, dict):
        return False
    if hook.get("type") == "http":
        return str(hook.get("url", "")).endswith(CLAUDE_HOOK_PATH)
    if hook.get("type") == "command":
        return CLAUDE_HOOK_PATH in str(hook.get("command", ""))
    return False


def without_octomate_hooks(groups: JsonValue) -> list[JsonValue]:
    """An event's matcher groups with Octomate's handlers removed — every other hook is
    kept, and only groups left with no hooks are dropped."""
    if not isinstance(groups, list):
        return []
    kept: list[JsonValue] = []
    for group in groups:
        if not isinstance(group, dict):
            kept.append(group)
            continue
        handlers = group.get("hooks", [])
        if not isinstance(handlers, list):
            kept.append(group)
            continue
        remaining = [hook for hook in handlers if not is_octomate_hook(hook)]
        if not remaining:
            continue
        kept.append(group if remaining == handlers else {**group, "hooks": remaining})
    return kept


@hooks_typer.command("install")
def install(
    url: Annotated[
        str | None,
        typer.Option(
            help="Full hook URL to pin. Without it, hooks resolve "
            f"${CLISettings.env('url')} from each session's environment at fire time."
        ),
    ] = None,
    scope: ScopeOption = Scope.user,
    settings: SettingsOption = None,
) -> None:
    """Point native Claude Code sessions at Octomate's hook router.

    The transcript-stream launcher (a command hook on UserPromptSubmit) installs
    unconditionally, the machine Octomate itself runs on included: the stream is the
    only assembler — the server never reads a transcript from disk — so a session
    without a tail keeps only the hooks' sketch. Merges into the settings file:
    other hooks are preserved, and re-running replaces a stale Octomate handler in
    place rather than stacking another.
    """
    path = settings_file(scope, settings)
    document = load_settings(path)
    hooks = document.setdefault("hooks", {})
    if not isinstance(hooks, dict):
        raise typer.BadParameter(f"{path} has a non-object 'hooks' section")

    group: JsonValue = {"hooks": [claude_emit_handler(url)]}
    # The transcript-stream launcher rides the same event the ledger's first write
    # does: by the time it fires, the forwarding hook has already created the session
    # server-side, and the tail it spawns is deduplicated per session.
    launcher_group: JsonValue = {"hooks": [claude_launch_handler(url)]}
    # Every event present, not just the handled ones: an event Octomate once registered
    # and no longer does (`SessionStart`) would otherwise keep a stale handler forever.
    for event in {*hooks, *HANDLED_HOOK_EVENTS}:
        kept = without_octomate_hooks(hooks.get(event))
        if event in HANDLED_HOOK_EVENTS:
            kept.append(group)
        if event == "UserPromptSubmit":
            kept.append(launcher_group)
        if kept:
            hooks[event] = kept
        else:
            del hooks[event]
    write_settings(path, document)

    target = url if url is not None else f"${CLISettings.env('url')} at fire time"
    typer.echo(f"Installed Octomate hooks → {target}")
    typer.echo(f"  events:   {', '.join(HANDLED_HOOK_EVENTS)}")
    stream = (
        stream_url_for(url)
        if url is not None
        else f"derived from ${CLISettings.env('url')}"
    )
    typer.echo(f"  stream:   {stream} (via {LAUNCH_SCRIPT.name})")
    typer.echo(f"  settings: {path}")
    typer.echo(
        f"  auth:     Bearer ${{{CLISettings.env('secret')}}} from the environment"
    )
    announce_secret()


@hooks_typer.command("uninstall")
def uninstall(scope: ScopeOption = Scope.user, settings: SettingsOption = None) -> None:
    """Remove Octomate's hook handlers from a Claude settings file, leaving any other
    hooks untouched."""
    path = settings_file(scope, settings)
    document = load_settings(path)
    hooks = document.get("hooks")
    if not isinstance(hooks, dict):
        typer.echo(f"No Octomate hooks in {path}")
        raise typer.Exit()

    for event in list(hooks):
        kept = without_octomate_hooks(hooks[event])
        if kept:
            hooks[event] = kept
        else:
            del hooks[event]
    if not hooks:
        document.pop("hooks", None)
    write_settings(path, document)
    typer.echo(f"Removed Octomate hooks from {path}")


@hooks_typer.command("show")
def show(scope: ScopeOption = Scope.user, settings: SettingsOption = None) -> None:
    """Show the Octomate hook handlers currently installed in a Claude settings file."""
    path = settings_file(scope, settings)
    document = load_settings(path)
    hooks = document.get("hooks")

    found: list[tuple[str, JsonObject]] = []
    if isinstance(hooks, dict):
        for event, groups in hooks.items():
            for group in groups if isinstance(groups, list) else []:
                if not isinstance(group, dict):
                    continue
                handlers = group.get("hooks", [])
                for hook in handlers if isinstance(handlers, list) else []:
                    if isinstance(hook, dict) and is_octomate_hook(hook):
                        found.append((event, hook))
    if not found:
        typer.echo(f"No Octomate hooks in {path}")
        raise typer.Exit()

    typer.echo(f"Octomate hooks in {path}:")
    for event, hook in found:
        target = hook.get("url") or hook.get("command")
        typer.echo(f"  {event}: {target} (timeout {hook.get('timeout')}s)")
