"""`octomate codex ...` — the client-side contract with a native Codex session and
the commands that install it."""

from __future__ import annotations

import json
import shlex
import sys
from enum import Enum
from pathlib import Path
from typing import Annotated

import typer

from octomate_cli.hooks import EMIT_SCRIPT, OCTOMATE_URL_ENV, announce_hook_secret
from octomate_cli.jsontypes import JsonObject

# Bound so a wedged or slow Octomate can never freeze someone's Codex session.
HOOK_TIMEOUT = 10

# The hook route's path as clients address it; the server inlines the literal in its
# route, and the tests that speak to it keep the two matching.
CODEX_HOOK_PATH = "/hooks/codex"

# The events the emit command forwards; unlike Claude's http handlers, Codex delivers
# SessionStart to command hooks, so it is registered too.
HANDLED_HOOK_EVENTS = (
    "SessionStart",
    "UserPromptSubmit",
    "Stop",
    "SubagentStart",
    "SubagentStop",
)

codex_typer = typer.Typer(
    help="Operate the native Codex integration.", no_args_is_help=True
)
hooks_typer = typer.Typer(
    help="Manage the Codex hook pipe into Octomate.", no_args_is_help=True
)
codex_typer.add_typer(hooks_typer, name="hooks")


class Scope(str, Enum):
    user = "user"
    project = "project"


def hooks_file(scope: Scope, path: Path | None) -> Path:
    if path is not None:
        return path
    root = Path.home() if scope is Scope.user else Path.cwd()
    return root / ".codex" / "hooks.json"


def load(path: Path) -> JsonObject:
    if not path.exists() or not path.read_text().strip():
        return {}
    value = json.loads(path.read_text())
    if not isinstance(value, dict):
        raise typer.BadParameter(f"{path} is not a JSON object")
    return value


def write(path: Path, value: JsonObject) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2) + "\n")


def is_octomate_handler(value: object) -> bool:
    """A command hook aimed at Octomate's Codex hook path — matched by path, not the
    exact command, so a re-install replaces a stale handler whatever its host, port, or
    interpreter, including ones written by versions that ran `codex hooks emit`."""
    return (
        isinstance(value, dict)
        and value.get("type") == "command"
        and CODEX_HOOK_PATH in str(value.get("command", ""))
    )


@hooks_typer.command("install")
def install(
    url: Annotated[
        str | None,
        typer.Option(
            help="Full hook URL to pin. Without it, hooks resolve "
            f"${OCTOMATE_URL_ENV} from each session's environment at fire time."
        ),
    ] = None,
    scope: Annotated[Scope, typer.Option()] = Scope.user,
    path: Annotated[Path | None, typer.Option("--hooks-file")] = None,
) -> None:
    """Install observing command hooks without replacing the operator's hooks."""
    target = hooks_file(scope, path)
    document = load(target)
    hooks = document.setdefault("hooks", {})
    if not isinstance(hooks, dict):
        raise typer.BadParameter(f"{target} has a non-object 'hooks' section")
    # By absolute path, not `-m octomate...`: importing the package costs ~1.9s, which
    # Codex would pay on every blocking hook. Through `sys.executable` so the hook runs
    # on this interpreter, not whichever `python` the session's PATH resolves.
    parts = [sys.executable, str(EMIT_SCRIPT), "--path", CODEX_HOOK_PATH]
    if url is not None:
        parts += ["--url", url]
    command = shlex.join(parts)
    group: JsonObject = {
        "hooks": [{"type": "command", "command": command, "timeout": HOOK_TIMEOUT}]
    }
    for event in HANDLED_HOOK_EVENTS:
        groups = hooks.get(event)
        kept = []
        if isinstance(groups, list):
            for existing in groups:
                if not isinstance(existing, dict):
                    kept.append(existing)
                    continue
                handlers = existing.get("hooks")
                if not isinstance(handlers, list):
                    kept.append(existing)
                    continue
                remaining = [
                    handler for handler in handlers if not is_octomate_handler(handler)
                ]
                if remaining:
                    kept.append({**existing, "hooks": remaining})
        hooks[event] = [*kept, group]
    write(target, document)
    hook_target = url if url is not None else f"${OCTOMATE_URL_ENV} at fire time"
    typer.echo(f"Installed Octomate Codex hooks in {target} → {hook_target}")
    typer.echo(f"  events: {', '.join(HANDLED_HOOK_EVENTS)}")
    typer.echo(f"  emit:   {EMIT_SCRIPT}")
    typer.echo("Open /hooks in Codex and trust the new command hooks.")
    announce_hook_secret()


@hooks_typer.command("uninstall")
def uninstall(
    scope: Annotated[Scope, typer.Option()] = Scope.user,
    path: Annotated[Path | None, typer.Option("--hooks-file")] = None,
) -> None:
    target = hooks_file(scope, path)
    document = load(target)
    hooks = document.get("hooks")
    if isinstance(hooks, dict):
        for event in list(hooks):
            groups = hooks[event]
            kept = []
            if isinstance(groups, list):
                for group in groups:
                    if not isinstance(group, dict):
                        kept.append(group)
                        continue
                    handlers = group.get("hooks")
                    if not isinstance(handlers, list):
                        kept.append(group)
                        continue
                    remaining = [
                        handler
                        for handler in handlers
                        if not is_octomate_handler(handler)
                    ]
                    if remaining:
                        kept.append({**group, "hooks": remaining})
            if kept:
                hooks[event] = kept
            else:
                del hooks[event]
        if not hooks:
            document.pop("hooks", None)
    write(target, document)
    typer.echo(f"Removed Octomate Codex hooks from {target}")
