"""`octomate claude ...` — operator commands for the native Claude Code integration.

Everything CLI-shaped lives under `octomate/cli`, the tentacle keeps only runtime;
the hook-path scripts beside this module (`launch.py`, `emit.py`) additionally stay
stdlib-only and are run by path, because a hook pays their startup on every fire.
"""

from __future__ import annotations

import json
import shlex
import sys
from enum import Enum
from pathlib import Path
from typing import Annotated

import typer

from octomate.cli.hooks import HOOK_SECRET_ENV, announce_hook_secret
from octomate.tentacles.agents.claude.hooks import (
    CLAUDE_HOOK_PATH,
    CLAUDE_STREAM_PATH,
    HANDLED_HOOK_EVENTS,
    HOOK_TIMEOUT,
)
from octomate.types.json import JsonObject, JsonValue

# The launcher command hook's script, run by absolute path so it never imports the
# package (see its module docstring); the same pattern as codex's EMIT_SCRIPT.
LAUNCH_SCRIPT = Path(__file__).with_name("launch.py")

claude_typer = typer.Typer(
    help="Operate the native Claude Code integration.", no_args_is_help=True
)
hooks_typer = typer.Typer(
    help="Manage the Claude Code hook pipe into Octomate.", no_args_is_help=True
)
claude_typer.add_typer(hooks_typer, name="hooks")


@claude_typer.callback()
def claude_group() -> None:
    # Hint without blocking: the commands still run, since a machine may be set up ahead
    # of the config.
    from octomate.config import OctomateConfig

    if OctomateConfig().agents.claude is None:
        typer.secho(
            "Note: config.agents.claude is unset, so no hook router is served.",
            fg=typer.colors.YELLOW,
            err=True,
        )


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


def configured_hook_url() -> str:
    """The hook URL from the running config's host/port. A wildcard bind address is not
    reachable as a URL, so it collapses to loopback — the Claude client is local."""
    from octomate.config import OctomateConfig  # heavy + optional; only when needed

    config = OctomateConfig()
    host = str(config.host)
    if host in {"0.0.0.0", "::"}:
        host = "127.0.0.1"
    return f"http://{host}:{config.port}{CLAUDE_HOOK_PATH}"


def claude_hook_handler(url: str) -> JsonObject:
    """One `http` handler: POST the event body to `url`, bearing the hook credential.

    The credential is a `${VAR}` reference, not its value — Claude Code resolves it at
    fire time, and only for names in `allowedEnvVars` — so a settings file stays safe to
    commit. Synchronous, which is what guarantees delivery before a short-lived
    `claude -p` exits.
    """
    return {
        "type": "http",
        "url": url,
        "timeout": HOOK_TIMEOUT,
        "headers": {"Authorization": f"Bearer ${{{HOOK_SECRET_ENV}}}"},
        "allowedEnvVars": [HOOK_SECRET_ENV],
    }


def stream_url_for(hook_url: str) -> str:
    """The stream endpoint the hook URL implies: same host, ws(s) for http(s)."""
    base = hook_url.removesuffix(CLAUDE_HOOK_PATH)
    scheme, _, rest = base.partition("://")
    return f"{'wss' if scheme == 'https' else 'ws'}://{rest}{CLAUDE_STREAM_PATH}"


def claude_launch_handler(hook_url: str) -> JsonObject:
    """The launcher `command` hook: spawns `octomate claude tail` for the session,
    detached (`launch.py`). A command hook because only a local process can start
    one — the `http` handlers reach Octomate but can start nothing on this machine.
    The command pins this installer's own interpreter and octomate script by absolute
    path, so it works from whatever shell Claude runs hooks in; the tail reads the
    credential from the environment, so none is written here."""
    command = shlex.join(
        [
            sys.executable,
            str(LAUNCH_SCRIPT),
            "--url",
            stream_url_for(hook_url),
            "--octomate",
            str(Path(sys.argv[0]).resolve()),
        ]
    )
    return {"type": "command", "command": command, "timeout": HOOK_TIMEOUT}


def is_octomate_hook(hook: JsonValue) -> bool:
    """A handler this installer wrote: the `http` handler pointing at Octomate's hook
    path, or the launcher `command` naming its launch script. Matched loosely (path
    suffix, script name) so a re-install with a changed host/port replaces the stale
    one."""
    if not isinstance(hook, dict):
        return False
    if hook.get("type") == "http":
        return str(hook.get("url", "")).endswith(CLAUDE_HOOK_PATH)
    if hook.get("type") == "command":
        return str(LAUNCH_SCRIPT) in str(hook.get("command", ""))
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
        typer.Option(help="Full hook URL; defaults to the configured host/port."),
    ] = None,
    scope: ScopeOption = Scope.user,
    settings: SettingsOption = None,
) -> None:
    """Point native Claude Code sessions at Octomate's hook router.

    Merges into the settings file: other hooks are preserved, and re-running replaces a
    stale Octomate handler in place rather than stacking another.
    """
    hook_url = url or configured_hook_url()
    path = settings_file(scope, settings)
    document = load_settings(path)
    hooks = document.setdefault("hooks", {})
    if not isinstance(hooks, dict):
        raise typer.BadParameter(f"{path} has a non-object 'hooks' section")

    group: JsonValue = {"hooks": [claude_hook_handler(hook_url)]}
    # The transcript-stream launcher rides the same event the ledger's first write
    # does: by the time it fires, the http hook has already created the session
    # server-side, and the tail it spawns is deduplicated per session.
    launcher: JsonValue = {"hooks": [claude_launch_handler(hook_url)]}
    # Every event present, not just the handled ones: an event Octomate once registered
    # and no longer does (`SessionStart`) would otherwise keep a stale handler forever.
    for event in {*hooks, *HANDLED_HOOK_EVENTS}:
        kept = without_octomate_hooks(hooks.get(event))
        if event in HANDLED_HOOK_EVENTS:
            kept.append(group)
        if event == "UserPromptSubmit":
            kept.append(launcher)
        if kept:
            hooks[event] = kept
        else:
            del hooks[event]
    write_settings(path, document)

    typer.echo(f"Installed Octomate hooks → {hook_url}")
    typer.echo(f"  events:   {', '.join(HANDLED_HOOK_EVENTS)}")
    typer.echo(f"  stream:   {stream_url_for(hook_url)} (via {LAUNCH_SCRIPT.name})")
    typer.echo(f"  settings: {path}")
    typer.echo(f"  auth:     Authorization: Bearer ${{{HOOK_SECRET_ENV}}}")
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
        str,
        typer.Option(
            help="Octomate stream URL (ws://<host>:<port>/hooks/claude/stream)."
        ),
    ],
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
    the hook credential from the environment, like every hook client does.
    """
    from octomate.cli.tail import main  # heavy; only when tailing

    main(session_id=session, transcript_path=path, url=url, cwd=cwd)
