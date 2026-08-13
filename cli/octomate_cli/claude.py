"""`octomate claude ...` — the client-side contract with a native Claude Code session
and the commands that install it.

The hook-path scripts beside this module (`launch.py`, `emit.py`) stay stdlib-only
and are run by path, because a hook pays their startup on every fire.
"""

from __future__ import annotations

import json
import shlex
import sys
from enum import Enum
from pathlib import Path
from typing import Annotated, Literal

import typer

from octomate_cli.config import (
    HOOK_SECRET_ENV,
    OCTOMATE_URL_ENV,
    resolved_url,
)
from octomate_cli.hooks import EMIT_SCRIPT, LAUNCH_SCRIPT, announce_hook_secret
from octomate_cli.jsontypes import JsonObject, JsonValue

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

claude_typer = typer.Typer(
    help="Operate the native Claude Code integration.", no_args_is_help=True
)
hooks_typer = typer.Typer(
    help="Manage the Claude Code hook pipe into Octomate.", no_args_is_help=True
)
claude_typer.add_typer(hooks_typer, name="hooks")


class Scope(str, Enum):
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
    address (`OCTOMATE_URL`) — from the environment at fire time. A command rather
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
    install pinned `--url`, and otherwise resolved from `OCTOMATE_URL` at fire time,
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


def load_settings(path: Path) -> JsonObject:
    if not path.exists() or not path.read_text().strip():
        return {}
    data = json.loads(path.read_text())
    if not isinstance(data, dict):
        raise typer.BadParameter(f"{path} is not a JSON object")
    return data


def write_settings(path: Path, settings: JsonObject) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(settings, indent=2) + "\n")


@hooks_typer.command("install")
def install(
    url: Annotated[
        str | None,
        typer.Option(
            help="Full hook URL to pin. Without it, hooks resolve "
            f"${OCTOMATE_URL_ENV} from each session's environment at fire time."
        ),
    ] = None,
    scope: ScopeOption = Scope.user,
    settings: SettingsOption = None,
    launcher: Annotated[
        bool,
        typer.Option(
            "--launcher/--no-launcher",
            help="Also install the transcript-stream launcher (a command hook on "
            "UserPromptSubmit). Skip it on the machine Octomate itself runs on: "
            "local sessions are tailed from disk, and a spawned tail would only "
            "be refused.",
        ),
    ] = True,
) -> None:
    """Point native Claude Code sessions at Octomate's hook router.

    Merges into the settings file: other hooks are preserved, and re-running replaces a
    stale Octomate handler in place rather than stacking another — so re-running with
    `--no-launcher` also retires a launcher a previous install left.
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
        if event == "UserPromptSubmit" and launcher:
            kept.append(launcher_group)
        if kept:
            hooks[event] = kept
        else:
            del hooks[event]
    write_settings(path, document)

    target = url if url is not None else f"${OCTOMATE_URL_ENV} at fire time"
    typer.echo(f"Installed Octomate hooks → {target}")
    typer.echo(f"  events:   {', '.join(HANDLED_HOOK_EVENTS)}")
    if launcher:
        stream = (
            stream_url_for(url)
            if url is not None
            else f"derived from ${OCTOMATE_URL_ENV}"
        )
        typer.echo(f"  stream:   {stream} (via {LAUNCH_SCRIPT.name})")
    typer.echo(f"  settings: {path}")
    typer.echo(f"  auth:     Bearer ${{{HOOK_SECRET_ENV}}} from the environment")
    announce_hook_secret()


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


@claude_typer.command("tail")
def tail(
    session: Annotated[str, typer.Option(help="Native session id to stream.")],
    path: Annotated[
        Path, typer.Option(help="The session's transcript path on this machine.")
    ],
    url: Annotated[
        str | None,
        typer.Option(
            help="Octomate stream URL (ws://<host>:<port>/hooks/claude/stream); "
            f"defaults to one derived from ${OCTOMATE_URL_ENV}."
        ),
    ] = None,
    cwd: Annotated[
        str,
        typer.Option(
            help="Directory the session runs in; files its thread under a project."
        ),
    ] = "",
) -> None:
    """Stream a native session's transcript to Octomate, raw lines over one socket.

    Spawned per session by the launcher hook; safe to run by hand for a backfill —
    the server states where each file resumes, so re-running never duplicates. Reads
    the hook credential — and, absent `--url`, the server's address — from the
    environment, like every hook client does.
    """
    if url is None:
        base = resolved_url()
        if base is None:
            raise typer.BadParameter(
                f"no --url given, {OCTOMATE_URL_ENV} is unset, and no cli.toml "
                "names a url — one of them must say where Octomate is"
            )
        url = stream_url_for(base.rstrip("/") + CLAUDE_HOOK_PATH)
    from octomate_cli.tail import main  # watchfiles/websockets; only when tailing

    main(session_id=session, transcript_path=path, url=url, cwd=cwd)
