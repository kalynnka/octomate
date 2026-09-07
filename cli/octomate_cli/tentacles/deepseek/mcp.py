"""Install and manage the deepseek tentacle's MCP configuration."""

from __future__ import annotations

import json
from typing import Annotated

import typer

from octomate_cli.config import CLISettings
from octomate_cli.tentacles.deepseek.config import (
    DshHomeOption,
    dsh_home,
    patch_file,
    patch_text_with_block,
    patch_text_without_block,
)
from octomate_cli.tentacles.mcp import (
    CLIENT_HEADER,
    DEEPSEEK_NATIVE_CLIENT,
    OCTOMATE_SERVER_KEY,
    octomate_secret,
    octomate_url,
)

mcp_typer = typer.Typer(
    help="Manage the Octomate MCP row for native dsh sessions.", no_args_is_help=True
)


# The MCP client bridge the Octomate row mounts — part of dsh's own module
# closure, unlike the hooks bridge, so no --bridge link step.
MCP_CLIENT_PACKAGE = "@deepseek-ai/dsh-mcp-client"


# The Octomate MCP row's id and markers: what a re-install overwrites and an
# uninstall removes, without ever touching the hooks block.
MCP_ROW_ID = "octomate-mcp"


MCP_MARK_BEGIN = "# >>> octomate deepseek mcp >>>"


MCP_MARK_END = "# <<< octomate deepseek mcp <<<"


LEGACY_MCP_MARK_BEGIN = "# >>> octomate deepseek gateway >>>"


LEGACY_MCP_MARK_END = "# <<< octomate deepseek gateway <<<"


def mcp_patch_block(url: str, secret: str) -> str:
    """The marker-delimited row mounting dsh's MCP client on the served server —
    `serverName: octomate`, so the model sees `mcp__octomate__<tool>`, the same
    names Claude and Codex read. Header values are JSON-quoted, which YAML reads
    as flow scalars: the credential is hand-written and need not be YAML-safe."""
    return (
        f"{MCP_MARK_BEGIN}\n"
        f"- insert:\n"
        f"    - id: {MCP_ROW_ID}\n"
        f"      name: '{MCP_CLIENT_PACKAGE}'\n"
        f"      config:\n"
        f"        serverName: {OCTOMATE_SERVER_KEY}\n"
        f"        transport: streamable-http\n"
        f"        url: {json.dumps(url)}\n"
        f"        headers:\n"
        f"          Authorization: {json.dumps(f'Bearer {secret}')}\n"
        f"          {CLIENT_HEADER}: {DEEPSEEK_NATIVE_CLIENT}\n"
        f"{MCP_MARK_END}\n"
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
    text = patch_text_without_block(text, LEGACY_MCP_MARK_BEGIN, LEGACY_MCP_MARK_END)
    patch.parent.mkdir(parents=True, exist_ok=True)
    patch.write_text(
        patch_text_with_block(
            text,
            mcp_patch_block(target, secret),
            MCP_MARK_BEGIN,
            MCP_MARK_END,
        )
    )
    typer.echo(f"Installed the Octomate MCP row → {target}")
    typer.echo(f"  patch:  {patch} (row id {MCP_ROW_ID!r})")
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
    text = patch_text_without_block(patch.read_text(), MCP_MARK_BEGIN, MCP_MARK_END)
    patch.write_text(
        patch_text_without_block(text, LEGACY_MCP_MARK_BEGIN, LEGACY_MCP_MARK_END)
    )
    typer.echo(f"Removed the Octomate MCP row from {patch}")


@mcp_typer.command("show")
def mcp_show(home: DshHomeOption = None) -> None:
    """Show the Octomate MCP row, credential masked."""
    patch = patch_file(dsh_home(home))
    text = patch.read_text() if patch.exists() else ""
    lines = text.splitlines()
    markers = (
        (MCP_MARK_BEGIN, MCP_MARK_END)
        if MCP_MARK_BEGIN in (line.strip() for line in lines)
        else (LEGACY_MCP_MARK_BEGIN, LEGACY_MCP_MARK_END)
    )
    if markers[0] not in (line.strip() for line in lines):
        typer.echo(f"No Octomate MCP row in {patch}")
        raise typer.Exit()
    typer.echo(f"Octomate MCP row in {patch}:")
    inside = False
    for line in lines:
        stripped = line.strip()
        if stripped == markers[0]:
            inside = True
            continue
        if stripped == markers[1]:
            break
        if inside:
            if stripped.startswith("Authorization:"):
                indent = line[: len(line) - len(line.lstrip())]
                line = f'{indent}Authorization: "Bearer ***"'
            typer.echo(f"  {line}")
