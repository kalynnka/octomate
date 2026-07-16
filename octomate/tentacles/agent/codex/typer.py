from __future__ import annotations

import json
import os
import shlex
import sys
from enum import Enum
from pathlib import Path
from typing import Annotated

import httpx
import typer

from octomate.tentacles.agent.codex.hooks import (
    CODEX_HOOK_PATH,
    DRIVEN_ENV,
    HANDLED_HOOK_EVENTS,
    HOOK_TIMEOUT,
)
from octomate.types.json import JsonObject

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


def configured_hook_url() -> str:
    from octomate.config import OctomateConfig

    config = OctomateConfig()
    host = str(config.host)
    if host in {"0.0.0.0", "::"}:
        host = "127.0.0.1"
    return f"http://{host}:{config.port}{CODEX_HOOK_PATH}"


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
    return (
        isinstance(value, dict)
        and value.get("type") == "command"
        and "codex hooks emit" in str(value.get("command", ""))
    )


@hooks_typer.command("install")
def install(
    url: Annotated[str | None, typer.Option()] = None,
    scope: Annotated[Scope, typer.Option()] = Scope.user,
    path: Annotated[Path | None, typer.Option("--hooks-file")] = None,
) -> None:
    """Install observing command hooks without replacing the operator's hooks."""
    target = hooks_file(scope, path)
    document = load(target)
    hooks = document.setdefault("hooks", {})
    if not isinstance(hooks, dict):
        raise typer.BadParameter(f"{target} has a non-object 'hooks' section")
    hook_url = url or configured_hook_url()
    command = shlex.join(["octomate", "codex", "hooks", "emit", "--url", hook_url])
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
    typer.echo(f"Installed Octomate Codex hooks in {target}")
    typer.echo("Open /hooks in Codex and trust the new command hooks.")


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


@hooks_typer.command("emit", hidden=True)
def emit(url: Annotated[str, typer.Option()]) -> None:
    """Forward Codex's stdin hook payload to the local Octomate router."""
    payload = json.load(sys.stdin)
    if os.environ.get(DRIVEN_ENV) == "1":
        payload["octomate_driven"] = True
    response = httpx.post(url, json=payload, timeout=HOOK_TIMEOUT)
    response.raise_for_status()
    typer.echo("{}")
