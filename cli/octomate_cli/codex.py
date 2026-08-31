"""`octomate codex ...` — the client-side contract with a native Codex session and
the commands that install it."""

from __future__ import annotations

import json
import shlex
import sys
from enum import Enum
from pathlib import Path
from typing import Annotated

import tomlkit
import typer
from tomlkit.items import Table

from octomate_cli.config import OCTOMATE_URL_ENV, SECRET_ENV, resolved_url
from octomate_cli.hooks import EMIT_SCRIPT, LAUNCH_SCRIPT, announce_secret
from octomate_cli.jsontypes import JsonObject
from octomate_cli.mcp import (
    CLIENT_HEADER,
    CODEX_NATIVE_CLIENT,
    GATEWAY_SERVER_KEY,
    gateway_url,
)

# Bound so a wedged or slow Octomate can never freeze someone's Codex session.
HOOK_TIMEOUT = 10

# The hook route's path as clients address it; the server inlines the literal in its
# route, and the tests that speak to it keep the two matching.
CODEX_HOOK_PATH = "/hooks/codex"

# The transcript stream's path, likewise: a tail connects to
# `ws://<host>:<port>{CODEX_STREAM_PATH}` bearing the same hook credential.
CODEX_STREAM_PATH = "/hooks/codex/stream"

# The events the emit command forwards; unlike Claude's http handlers, Codex delivers
# SessionStart to command hooks, so it is registered too.
HANDLED_HOOK_EVENTS = (
    "SessionStart",
    "UserPromptSubmit",
    "Stop",
    "SubagentStart",
    "SubagentStop",
)

# Where the transcript-stream launcher rides. The session events spawn the tail; the
# stop event is the one whose `agent_transcript_path` names a child rollout — the tail
# cannot tell which sibling file is a child of its session, so the hook that knows
# hands it the path. `SubagentStart` carries no path, so it spawns nothing new.
LAUNCHER_HOOK_EVENTS = ("SessionStart", "UserPromptSubmit", "SubagentStop")

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
    """A command hook aimed at Octomate's Codex hook path (which the stream path
    extends, so pinned launchers match too) — matched by path, not the exact command,
    so a re-install replaces a stale handler whatever its host, port, or interpreter,
    including ones written by versions that ran `codex hooks emit`."""
    return (
        isinstance(value, dict)
        and value.get("type") == "command"
        and CODEX_HOOK_PATH in str(value.get("command", ""))
    )


def stream_url_for(hook_url: str) -> str:
    """The stream endpoint the hook URL implies: same host, ws(s) for http(s)."""
    base = hook_url.removesuffix(CODEX_HOOK_PATH)
    scheme, _, rest = base.partition("://")
    return f"{'wss' if scheme == 'https' else 'ws'}://{rest}{CODEX_STREAM_PATH}"


def codex_launch_handler(hook_url: str | None) -> JsonObject:
    """The launcher `command` hook: spawns `octomate codex tail` for the session,
    detached (`launch.py`) — the forwarding hooks reach Octomate but can start
    nothing on this machine, and the stream needs a local process. The command pins
    this installer's own interpreter and octomate script by absolute path, so it
    works from whatever shell Codex runs hooks in; the stream address is pinned only
    when the install pinned `--url`, and otherwise resolved from `OCTOMATE_URL` at
    fire time, like the credential always is."""
    command = [
        sys.executable,
        str(LAUNCH_SCRIPT),
        "--path",
        CODEX_HOOK_PATH,
        "--agent",
        "codex",
        "--octomate",
        str(Path(sys.argv[0]).resolve()),
    ]
    if hook_url is not None:
        command += ["--url", stream_url_for(hook_url)]
    return {"type": "command", "command": shlex.join(command), "timeout": HOOK_TIMEOUT}


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
    """Install observing command hooks without replacing the operator's hooks.

    The transcript-stream launcher installs unconditionally, the machine Octomate
    itself runs on included: the stream is the only assembler — the server never
    reads a rollout from disk — so a session without a tail keeps only the hooks'
    sketch. Re-running replaces a stale Octomate handler in place rather than
    stacking another.
    """
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
    launcher_group: JsonObject = {"hooks": [codex_launch_handler(url)]}
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
        installed = [*kept, group]
        if event in LAUNCHER_HOOK_EVENTS:
            installed.append(launcher_group)
        hooks[event] = installed
    write(target, document)
    hook_target = url if url is not None else f"${OCTOMATE_URL_ENV} at fire time"
    typer.echo(f"Installed Octomate Codex hooks in {target} → {hook_target}")
    typer.echo(f"  events: {', '.join(HANDLED_HOOK_EVENTS)}")
    typer.echo(f"  emit:   {EMIT_SCRIPT}")
    stream = (
        stream_url_for(url) if url is not None else f"derived from ${OCTOMATE_URL_ENV}"
    )
    typer.echo(f"  stream: {stream} (via {LAUNCH_SCRIPT.name})")
    typer.echo("Open /hooks in Codex and trust the new command hooks.")
    announce_secret()


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


@codex_typer.command("tail")
def tail(
    session: Annotated[str, typer.Option(help="Native session id to stream.")],
    path: Annotated[
        Path, typer.Option(help="The session's rollout path on this machine.")
    ],
    url: Annotated[
        str | None,
        typer.Option(
            help="Octomate stream URL (ws://<host>:<port>/hooks/codex/stream); "
            f"defaults to one derived from ${OCTOMATE_URL_ENV}."
        ),
    ] = None,
    cwd: Annotated[
        str,
        typer.Option(
            help="Directory the session runs in; files its thread under a project."
        ),
    ] = "",
    agent_path: Annotated[
        Path | None,
        typer.Option(
            help="A child rollout to spool for the session's tail — what the "
            "launcher passes through from a SubagentStop hook."
        ),
    ] = None,
) -> None:
    """Stream a native Codex session's rollout to Octomate, raw lines over one socket.

    Spawned per session by the launcher hook; safe to run by hand for a backfill —
    committed turns are skipped server-side, so re-running never duplicates. Reads
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
        url = stream_url_for(base.rstrip("/") + CODEX_HOOK_PATH)
    from octomate_cli.tail import main, spool_path  # watchfiles; only when tailing

    main(
        session_id=session,
        transcript_path=path,
        url=url,
        cwd=cwd,
        spool=spool_path(session),
        agent_path=agent_path,
    )


mcp_typer = typer.Typer(
    help="Manage the gateway MCP entry for native Codex sessions.",
    no_args_is_help=True,
)
codex_typer.add_typer(mcp_typer, name="mcp")

McpConfigOption = Annotated[
    Path | None,
    typer.Option(
        "--config-file",
        help="Explicit config.toml path; defaults to ~/.codex/config.toml.",
    ),
]


def mcp_config_file(path: Path | None) -> Path:
    return path if path is not None else Path.home() / ".codex" / "config.toml"


def load_toml(path: Path) -> tomlkit.TOMLDocument:
    """The whole document, comments and all — tomlkit's round-trip is what keeps
    the operator's file theirs."""
    return tomlkit.parse(path.read_text()) if path.exists() else tomlkit.document()


@mcp_typer.command("install")
def mcp_install(
    url: Annotated[
        str | None,
        typer.Option(
            help="Octomate's base URL (http://host:port) to write; defaults to "
            f"${OCTOMATE_URL_ENV}, then cli.toml."
        ),
    ] = None,
    config_file: McpConfigOption = None,
) -> None:
    """Point native Codex sessions at the served gateway.

    Writes the `mcp_servers.gateway` table — the gateway's URL, the credential
    as a `bearer_token_env_var` reference, and the runtime attribution header —
    preserving the operator's comments and every other table. Codex resolves the
    variable from each session's environment, so the secret must be exported in
    the shell profile. A driven turn's per-run overrides shadow this entry by
    name, its conversation header winning over the client header.
    """
    target = gateway_url(url)
    path = mcp_config_file(config_file)
    document = load_toml(path)
    servers = document.get("mcp_servers")
    if servers is None:
        servers = tomlkit.table(is_super_table=True)
        document["mcp_servers"] = servers
    if not isinstance(servers, Table):
        raise typer.BadParameter(f"{path} has a non-table 'mcp_servers' section")
    entry = tomlkit.table()
    entry["url"] = target
    entry["bearer_token_env_var"] = SECRET_ENV
    headers = tomlkit.inline_table()
    headers[CLIENT_HEADER] = CODEX_NATIVE_CLIENT
    entry["http_headers"] = headers
    servers[GATEWAY_SERVER_KEY] = entry
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(tomlkit.dumps(document))
    typer.echo(f"Installed the gateway MCP entry → {target}")
    typer.echo(f"  file:   {path}")
    typer.echo(f"  client: {CODEX_NATIVE_CLIENT}")
    typer.echo(
        f"  auth:   ${{{SECRET_ENV}}} from each session's environment — export "
        "it in your shell profile"
    )
    announce_secret()


@mcp_typer.command("uninstall")
def mcp_uninstall(config_file: McpConfigOption = None) -> None:
    """Remove the gateway MCP entry, leaving the operator's comments and every
    other server."""
    path = mcp_config_file(config_file)
    document = load_toml(path)
    servers = document.get("mcp_servers")
    if not isinstance(servers, Table) or GATEWAY_SERVER_KEY not in servers:
        typer.echo(f"No gateway MCP entry in {path}")
        raise typer.Exit()
    del servers[GATEWAY_SERVER_KEY]
    if len(servers) == 0:
        del document["mcp_servers"]
    path.write_text(tomlkit.dumps(document))
    typer.echo(f"Removed the gateway MCP entry from {path}")


@mcp_typer.command("show")
def mcp_show(config_file: McpConfigOption = None) -> None:
    """Show the gateway MCP entry. Nothing to mask: the entry names an
    environment variable rather than holding the credential."""
    path = mcp_config_file(config_file)
    servers = load_toml(path).get("mcp_servers")
    entry = servers.get(GATEWAY_SERVER_KEY) if isinstance(servers, Table) else None
    if not isinstance(entry, Table):
        typer.echo(f"No gateway MCP entry in {path}")
        raise typer.Exit()
    headers = entry.get("http_headers")
    client = headers.get(CLIENT_HEADER) if isinstance(headers, dict) else None
    typer.echo(f"Gateway MCP entry in {path}:")
    typer.echo(f"  url:    {entry.get('url')}")
    typer.echo(f"  auth:   ${{{entry.get('bearer_token_env_var')}}}")
    typer.echo(f"  client: {client}")
