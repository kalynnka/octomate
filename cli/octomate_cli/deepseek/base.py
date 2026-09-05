"""`octomate deepseek ...` — the client-side contract with a native dsh session
and the commands that install it.

dsh has no hook protocol of its own; its `dsh-hooks-claude-code` bridge plugin
runs a Claude-Code-shaped hooks config on dsh's interception seams. So the
install writes two things: a hooks file registering the same stdlib emit
command Claude's and Codex's installers use (`emit.py`, run by absolute path),
and a row in `$DSH_HOME/cordis.patch.yml` mounting the bridge over it — dsh
composes its plugin tree from patch layers, and the home-level patch applies to
every dsh process sharing that home, `dsh web` daemon and terminal CLI alike.

The transcript itself travels like Claude's and Codex's — a per-session tail
process streams it to Octomate's stream endpoint — but `octomate deepseek
tail` reads no file: dsh's session log is zstd-framed and only advances at
checkpoints, so the tail reads this machine's dsh gateway
(`session.history`, decoded and unpacked) and ships each event as a framed
line, seqs standing in for byte offsets. The server never speaks to this
machine's dsh.
"""

from __future__ import annotations

import json
import os
import shlex
import sys
from pathlib import Path
from typing import Annotated

import typer

from octomate_cli.config import CLISettings, cli_settings
from octomate_cli.hooks import EMIT_SCRIPT, LAUNCH_SCRIPT, announce_secret
from octomate_cli.jsontypes import JsonObject
from octomate_cli.mcp import (
    CLIENT_HEADER,
    DEEPSEEK_NATIVE_CLIENT,
    OCTOMATE_SERVER_KEY,
    octomate_secret,
    octomate_url,
)

# Bound so a wedged or slow Octomate can never freeze someone's dsh session —
# both registered events sit on blocking seams (pre-step and turn-stopping).
# dsh's hooks.json `timeout` is in seconds, like Claude's.
HOOK_TIMEOUT = 10

# The hook route's path as clients address it; the server inlines the literal in
# its route, and the tests that speak to it keep the two matching.
DEEPSEEK_HOOK_PATH = "/hooks/deepseek"

# The event stream's path, likewise: a tail connects to
# `ws://<host>:<port>{DEEPSEEK_STREAM_PATH}` bearing the same hook credential.
DEEPSEEK_STREAM_PATH = "/hooks/deepseek/stream"

# Where this machine's dsh gateway answers, resolved at fire time: the
# environment names it when the operator moved the port, else dsh's default
# bind — the same default `agents.deepseek.port` carries server-side.
DSH_URL_ENV = "DSH_API_URL"
DEFAULT_DSH_URL = "http://127.0.0.1:3080"

# The events the hooks file registers. `UserPromptSubmit` marks the session
# live (the server catches up its missed turns); `Stop` marks a turn boundary
# (the server settles it from the log). dsh's bridge delivers no `SessionEnd`
# and carries no per-turn key or answer, so these two are the whole contract.
HANDLED_HOOK_EVENTS = ("UserPromptSubmit", "Stop")

# The bridge plugin the patch row mounts, and where its module must resolve
# from: dsh's flat module fallback, which the harness heals with its own
# dependency closure — the bridge ships outside that closure, so the installer
# links it there itself (`--bridge`).
BRIDGE_PACKAGE = "@deepseek-ai/dsh-hooks-claude-code"

# The patch row's id: what a re-install overwrites and an uninstall removes.
PATCH_ROW_ID = "octomate-hooks"

MARK_BEGIN = "# >>> octomate deepseek hooks >>>"
MARK_END = "# <<< octomate deepseek hooks <<<"

deepseek_typer = typer.Typer(
    help="Operate the native dsh (DeepSeek Harness) integration.",
    no_args_is_help=True,
)
hooks_typer = typer.Typer(
    help="Manage the dsh hook pipe into Octomate.", no_args_is_help=True
)
deepseek_typer.add_typer(hooks_typer, name="hooks")

DshHomeOption = Annotated[
    Path | None,
    typer.Option(help="The dsh home to install into; defaults to $DSH_HOME or ~/.dsh."),
]


def dsh_home(path: Path | None) -> Path:
    if path is not None:
        return path
    env = os.environ.get("DSH_HOME")
    return Path(env).expanduser() if env else Path.home() / ".dsh"


def hooks_file(home: Path) -> Path:
    return home / "octomate-hooks.json"


def patch_file(home: Path) -> Path:
    return home / "cordis.patch.yml"


def bridge_link(home: Path) -> Path:
    return home / "profiles" / "node_modules" / BRIDGE_PACKAGE


def emit_handler(url: str | None) -> JsonObject:
    """The forwarding `command` hook: `emit.py` carries the event body from
    stdin to the hook router, reading the credential — and, unless `url` pins
    one, the router's address — from the environment at fire time."""
    command = [sys.executable, str(EMIT_SCRIPT), "--path", DEEPSEEK_HOOK_PATH]
    if url is not None:
        command += ["--url", url]
    return {
        "type": "command",
        "command": shlex.join(command),
        "timeout": HOOK_TIMEOUT,
    }


def stream_url_for(hook_url: str) -> str:
    """The stream endpoint the hook URL implies: same host, ws(s) for http(s)."""
    base = hook_url.removesuffix(DEEPSEEK_HOOK_PATH)
    scheme, _, rest = base.partition("://")
    return f"{'wss' if scheme == 'https' else 'ws'}://{rest}{DEEPSEEK_STREAM_PATH}"


def launch_handler(hook_url: str | None) -> JsonObject:
    """The launcher `command` hook: spawns `octomate deepseek tail` for the
    session, detached (`launch.py`) — the forwarding hook reaches Octomate but
    can start nothing on this machine, and the stream needs a local process to
    read the gateway. Pins this installer's own interpreter and octomate
    script by absolute path; the stream address is pinned only when the
    install pinned `--url`, and otherwise resolved from `OCTOMATE_CLI_URL` at fire
    time, like the credential always is."""
    command = [
        sys.executable,
        str(LAUNCH_SCRIPT),
        "--path",
        DEEPSEEK_HOOK_PATH,
        "--agent",
        "deepseek",
        "--octomate",
        str(Path(sys.argv[0]).resolve()),
    ]
    if hook_url is not None:
        command += ["--url", stream_url_for(hook_url)]
    return {"type": "command", "command": shlex.join(command), "timeout": HOOK_TIMEOUT}


def hooks_document(url: str | None) -> JsonObject:
    handler = emit_handler(url)
    # The launcher rides the prompt event only: by the time it fires, the emit
    # hook on the same event has already created the session server-side, and
    # the tail it spawns deduplicates itself per session.
    return {
        "hooks": {
            "UserPromptSubmit": [
                {"hooks": [handler]},
                {"hooks": [launch_handler(url)]},
            ],
            "Stop": [{"hooks": [handler]}],
        }
    }


def patch_block(config_path: Path) -> str:
    """The marker-delimited patch entry mounting the bridge over our hooks
    file. Textual rather than parsed YAML: the user's patch file carries their
    comments and `!!js` expressions, which a parse-and-rewrite would destroy."""
    return (
        f"{MARK_BEGIN}\n"
        f"- insert:\n"
        f"    - id: {PATCH_ROW_ID}\n"
        f"      name: '{BRIDGE_PACKAGE}'\n"
        f"      config:\n"
        f"        configPath: {json.dumps(str(config_path))}\n"
        f"{MARK_END}\n"
    )


def without_block(text: str, begin: str = MARK_BEGIN, end: str = MARK_END) -> str:
    """The patch file's text with one marker block removed, everything else
    kept byte-for-byte. Defaults to the hooks block's markers; the gateway
    block passes its own."""
    lines = text.splitlines(keepends=True)
    kept: list[str] = []
    inside = False
    for line in lines:
        stripped = line.strip()
        if stripped == begin:
            inside = True
            continue
        if stripped == end:
            inside = False
            continue
        if not inside:
            kept.append(line)
    return "".join(kept)


def patch_text_with_block(
    text: str, block: str, begin: str = MARK_BEGIN, end: str = MARK_END
) -> str:
    """The patch file's text with our block installed exactly once.

    The file is a top-level YAML array. dsh's default is a lone `[]` flow
    document, which nothing can be appended after — that line is replaced by
    the block. A file already carrying block-sequence entries gets the block
    appended; a re-install replaces the existing block in place.
    """
    remainder = without_block(text, begin, end)
    lines = remainder.splitlines(keepends=True)
    for index, line in enumerate(lines):
        if line.strip() == "[]":
            return "".join([*lines[:index], block, *lines[index + 1 :]])
    if remainder and not remainder.endswith("\n"):
        remainder += "\n"
    return remainder + block


def patch_text_without_block(
    text: str, begin: str = MARK_BEGIN, end: str = MARK_END
) -> str:
    """The uninstall splice: the block removed, and the empty-array document
    restored when nothing else remains — a comments-only file parses as null,
    not the empty entry list the loader expects."""
    remainder = without_block(text, begin, end)
    if any(
        line.strip() and not line.strip().startswith("#")
        for line in remainder.splitlines()
    ):
        return remainder
    if remainder and not remainder.endswith("\n"):
        remainder += "\n"
    return remainder + "[]\n"


def link_bridge(home: Path, bridge: Path) -> Path:
    """Symlink the bridge package into dsh's flat module fallback, where every
    profile resolves plugins from. Validated first: the wrong directory here
    would fail every dsh boot, not just the hooks."""
    manifest = bridge / "package.json"
    try:
        name = json.loads(manifest.read_text()).get("name")
    except (OSError, json.JSONDecodeError):
        raise typer.BadParameter(f"{bridge} holds no readable package.json") from None
    if name != BRIDGE_PACKAGE:
        raise typer.BadParameter(f"{bridge} is {name!r}, not {BRIDGE_PACKAGE!r}")
    if not (bridge / "lib" / "index.js").exists():
        raise typer.BadParameter(
            f"{bridge} has no lib/index.js — build the harness first"
        )
    link = bridge_link(home)
    link.parent.mkdir(parents=True, exist_ok=True)
    if link.is_symlink():
        link.unlink()
    elif link.exists():
        raise typer.BadParameter(
            f"{link} exists and is not a symlink; remove it so the installer "
            "can manage the bridge link"
        )
    link.symlink_to(bridge.resolve())
    return link


@hooks_typer.command("install")
def install(
    url: Annotated[
        str | None,
        typer.Option(
            help="Full hook URL to pin. Without it, hooks resolve "
            f"${CLISettings.env('url')} from each session's environment at fire time."
        ),
    ] = None,
    home: DshHomeOption = None,
    bridge: Annotated[
        Path | None,
        typer.Option(
            help="Path of the dsh-hooks-claude-code package to link into dsh's "
            "module fallback (e.g. <harness checkout>/packages/hooks/"
            "hooks-claude-code). The bridge ships outside dsh's own bundle, so "
            "the first install needs this once."
        ),
    ] = None,
) -> None:
    """Point native dsh sessions at Octomate's hook router.

    Writes Octomate's own hooks file (never a merge — an operator's dsh hooks
    live in their own file, mounted by their own patch row) and a
    marker-delimited row in $DSH_HOME/cordis.patch.yml mounting the
    `dsh-hooks-claude-code` bridge over it. Re-running replaces both in place.
    Restart dsh processes for the composition change to take.
    """
    target_home = dsh_home(home)
    config_path = hooks_file(target_home)
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(json.dumps(hooks_document(url), indent=2) + "\n")

    patch = patch_file(target_home)
    text = patch.read_text() if patch.exists() else "[]\n"
    patch.write_text(patch_text_with_block(text, patch_block(config_path)))

    if bridge is not None:
        link = link_bridge(target_home, bridge)
        typer.echo(f"Linked {BRIDGE_PACKAGE} → {link.readlink()}")
    elif not bridge_link(target_home).exists():
        typer.secho(
            f"\n{BRIDGE_PACKAGE} does not resolve from {bridge_link(target_home)} — "
            "dsh will fail to mount the hooks row until it does. Re-run with "
            "--bridge <harness checkout>/packages/hooks/hooks-claude-code.",
            fg=typer.colors.YELLOW,
            err=True,
        )

    hook_target = url if url is not None else f"${CLISettings.env('url')} at fire time"
    typer.echo(f"Installed Octomate dsh hooks → {hook_target}")
    typer.echo(f"  events: {', '.join(HANDLED_HOOK_EVENTS)}")
    typer.echo(f"  hooks:  {config_path}")
    typer.echo(f"  patch:  {patch} (row id {PATCH_ROW_ID!r})")
    stream = (
        stream_url_for(url)
        if url is not None
        else f"derived from ${CLISettings.env('url')}"
    )
    typer.echo(f"  stream: {stream} (via {LAUNCH_SCRIPT.name})")
    typer.echo("Restart dsh (the web daemon included) to load the bridge.")
    announce_secret()


@hooks_typer.command("uninstall")
def uninstall(home: DshHomeOption = None) -> None:
    """Remove Octomate's hooks file, patch row, and bridge link from a dsh
    home, leaving everything else in the patch file untouched."""
    target_home = dsh_home(home)
    patch = patch_file(target_home)
    if patch.exists():
        patch.write_text(patch_text_without_block(patch.read_text()))
    config_path = hooks_file(target_home)
    if config_path.exists():
        config_path.unlink()
    link = bridge_link(target_home)
    if link.is_symlink():
        link.unlink()
    typer.echo(f"Removed Octomate dsh hooks from {target_home}")


@deepseek_typer.command("tail")
def tail(
    session: Annotated[str, typer.Option(help="Native session id to stream.")],
    path: Annotated[
        Path,
        typer.Option(
            help="The session log's path on this machine — a recorded label; "
            "the tail reads the gateway, never this file."
        ),
    ] = Path(""),
    url: Annotated[
        str | None,
        typer.Option(
            help="Octomate stream URL (ws://<host>:<port>/hooks/deepseek/stream); "
            f"defaults to one derived from ${CLISettings.env('url')}."
        ),
    ] = None,
    cwd: Annotated[
        str,
        typer.Option(
            help="Directory the session runs in; files its thread under a project."
        ),
    ] = "",
    dsh_url: Annotated[
        str | None,
        typer.Option(
            help="This machine's dsh gateway; defaults to "
            f"${DSH_URL_ENV} or {DEFAULT_DSH_URL}."
        ),
    ] = None,
) -> None:
    """Stream a native dsh session's events to Octomate, read from this
    machine's dsh gateway rather than the zstd-framed log file.

    Spawned per session by the launcher hook; safe to run by hand for a
    backfill — the server states where the session resumes, so re-running
    never duplicates. Reads the hook credential — and, absent `--url`, the
    server's address — from the environment, like every hook client does.
    """
    if url is None:
        base = cli_settings().url
        if base is None:
            raise typer.BadParameter(
                f"no --url given, {CLISettings.env('url')} is unset, and no cli.toml "
                "names a url — one of them must say where Octomate is"
            )
        url = stream_url_for(base.rstrip("/") + DEEPSEEK_HOOK_PATH)
    if dsh_url is None:
        dsh_url = os.environ.get(DSH_URL_ENV) or DEFAULT_DSH_URL
    from octomate_cli.deepseek.tail import main  # websockets; only when tailing

    main(
        session_id=session,
        transcript_path=path,
        url=url,
        cwd=cwd,
        dsh_url=dsh_url,
    )


mcp_typer = typer.Typer(
    help="Manage the Octomate MCP row for native dsh sessions.", no_args_is_help=True
)
deepseek_typer.add_typer(mcp_typer, name="mcp")

# The MCP client bridge the Octomate row mounts — part of dsh's own module
# closure, unlike the hooks bridge, so no --bridge link step.
MCP_CLIENT_PACKAGE = "@deepseek-ai/dsh-mcp-client"

# The Octomate row's id and markers: what a re-install overwrites and an
# uninstall removes, without ever touching the hooks block above.
GATEWAY_ROW_ID = "octomate-gateway"
GATEWAY_MARK_BEGIN = "# >>> octomate deepseek gateway >>>"
GATEWAY_MARK_END = "# <<< octomate deepseek gateway <<<"


def gateway_patch_block(url: str, secret: str) -> str:
    """The marker-delimited row mounting dsh's MCP client on the served server —
    `serverName: octomate`, so the model sees `mcp__octomate__<tool>`, the same
    names Claude and Codex read. Header values are JSON-quoted, which YAML reads
    as flow scalars: the credential is hand-written and need not be YAML-safe."""
    return (
        f"{GATEWAY_MARK_BEGIN}\n"
        f"- insert:\n"
        f"    - id: {GATEWAY_ROW_ID}\n"
        f"      name: '{MCP_CLIENT_PACKAGE}'\n"
        f"      config:\n"
        f"        serverName: {OCTOMATE_SERVER_KEY}\n"
        f"        transport: streamable-http\n"
        f"        url: {json.dumps(url)}\n"
        f"        headers:\n"
        f"          Authorization: {json.dumps(f'Bearer {secret}')}\n"
        f"          {CLIENT_HEADER}: {DEEPSEEK_NATIVE_CLIENT}\n"
        f"{GATEWAY_MARK_END}\n"
    )


@mcp_typer.command("install")
def mcp_install(
    url: Annotated[
        str | None,
        typer.Option(
            help="Octomate's base URL (http://host:port) to write; defaults to "
            f"${CLISettings.env('url')}, then cli.toml."
        ),
    ] = None,
    home: DshHomeOption = None,
) -> None:
    """Point native dsh sessions at the served MCP server.

    Writes a marker-delimited row in $DSH_HOME/cordis.patch.yml mounting
    `@deepseek-ai/dsh-mcp-client` on the served MCP server, the credential and the
    runtime attribution embedded — resolved once, now: the file holds the
    literal credential, and rotating it means re-running install. Re-running
    replaces the row in place; restart dsh processes for the change to take.
    """
    target = octomate_url(url)
    secret = octomate_secret()
    patch = patch_file(dsh_home(home))
    text = patch.read_text() if patch.exists() else "[]\n"
    patch.parent.mkdir(parents=True, exist_ok=True)
    patch.write_text(
        patch_text_with_block(
            text,
            gateway_patch_block(target, secret),
            GATEWAY_MARK_BEGIN,
            GATEWAY_MARK_END,
        )
    )
    typer.echo(f"Installed the Octomate MCP row → {target}")
    typer.echo(f"  patch:  {patch} (row id {GATEWAY_ROW_ID!r})")
    typer.echo(f"  client: {DEEPSEEK_NATIVE_CLIENT}")
    typer.echo(
        "  auth:   embedded — the file holds the literal credential; rotation "
        "means re-running install"
    )
    typer.echo("Restart dsh (the web daemon included) to load the row.")


@mcp_typer.command("uninstall")
def mcp_uninstall(home: DshHomeOption = None) -> None:
    """Remove the Octomate MCP row, leaving the hooks row and everything else in
    the patch file untouched."""
    patch = patch_file(dsh_home(home))
    if not patch.exists():
        typer.echo(f"No Octomate MCP row in {patch}")
        raise typer.Exit()
    patch.write_text(
        patch_text_without_block(
            patch.read_text(), GATEWAY_MARK_BEGIN, GATEWAY_MARK_END
        )
    )
    typer.echo(f"Removed the Octomate MCP row from {patch}")


@mcp_typer.command("show")
def mcp_show(home: DshHomeOption = None) -> None:
    """Show the Octomate MCP row, credential masked."""
    patch = patch_file(dsh_home(home))
    text = patch.read_text() if patch.exists() else ""
    lines = text.splitlines()
    if GATEWAY_MARK_BEGIN not in (line.strip() for line in lines):
        typer.echo(f"No Octomate MCP row in {patch}")
        raise typer.Exit()
    typer.echo(f"Gateway MCP row in {patch}:")
    inside = False
    for line in lines:
        stripped = line.strip()
        if stripped == GATEWAY_MARK_BEGIN:
            inside = True
            continue
        if stripped == GATEWAY_MARK_END:
            break
        if inside:
            if stripped.startswith("Authorization:"):
                indent = line[: len(line) - len(line.lstrip())]
                line = f'{indent}Authorization: "Bearer ***"'
            typer.echo(f"  {line}")
