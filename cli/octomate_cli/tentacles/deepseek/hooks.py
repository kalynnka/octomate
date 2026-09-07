"""Install and manage the deepseek tentacle's native hooks."""

from __future__ import annotations

import json
import shlex
import sys
from pathlib import Path
from typing import Annotated

import typer

from octomate_cli.config import CLISettings
from octomate_cli.tentacles.deepseek.config import (
    MARK_BEGIN,
    MARK_END,
    DshHomeOption,
    dsh_home,
    patch_file,
    patch_text_with_block,
    patch_text_without_block,
)
from octomate_cli.tentacles.hooks import EMIT_SCRIPT, LAUNCH_SCRIPT, announce_secret
from octomate_cli.tentacles.types import JsonObject

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


hooks_typer = typer.Typer(
    help="Manage the dsh hook pipe into Octomate.", no_args_is_help=True
)


def hooks_file(home: Path) -> Path:
    return home / "octomate-hooks.json"


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
