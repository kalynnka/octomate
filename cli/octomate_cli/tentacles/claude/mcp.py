"""Install and manage the claude tentacle's MCP configuration."""

from __future__ import annotations

from enum import Enum
from pathlib import Path
from typing import Annotated

import typer

from octomate_cli.config import CLISettings
from octomate_cli.tentacles.claude.config import load_settings, write_settings
from octomate_cli.tentacles.claude.schema import McpContainer, McpServer
from octomate_cli.tentacles.mcp import (
    CLAUDE_NATIVE_CLIENT,
    CLIENT_HEADER,
    OCTOMATE_SERVER_KEY,
    octomate_secret,
    octomate_url,
)

mcp_typer = typer.Typer(
    help="Manage the Octomate MCP entry for native Claude Code sessions.",
    no_args_is_help=True,
)


# Preserve the existing enum stringification used by CLI option defaults.
class McpScope(str, Enum):  # noqa: UP042
    """Claude's three MCP config placements. `local` — the default, since the entry
    embeds a credential — is Claude's per-project slot inside `~/.claude.json`
    (`projects.<cwd>.mcpServers`, the shape `claude mcp add --scope local` writes):
    scoped to this directory without putting a secret-bearing file in the repo."""

    local = "local"
    user = "user"
    project = "project"


McpScopeOption = Annotated[
    McpScope,
    typer.Option(
        help="Where the entry lives: 'local' (~/.claude.json, this project only), "
        "'user' (~/.claude.json, every project), or 'project' (./.mcp.json)."
    ),
]


McpFileOption = Annotated[
    Path | None,
    typer.Option(
        "--file",
        help="Explicit config path; replaces the file --scope implies, while the "
        "scope keeps deciding where in it the entry lives.",
    ),
]


def mcp_config_file(scope: McpScope, file: Path | None) -> Path:
    if file is not None:
        return file
    if scope is McpScope.project:
        return Path.cwd() / ".mcp.json"
    return Path.home() / ".claude.json"


@mcp_typer.command("install")
def mcp_install(
    url: Annotated[
        str | None,
        typer.Option(
            help="Octomate's base URL (http://host:port) to write; defaults to "
            f"${CLISettings.env('url')}, then cli.toml."
        ),
    ] = None,
    scope: McpScopeOption = McpScope.local,
    file: McpFileOption = None,
) -> None:
    """Point native Claude Code sessions at the served MCP server.

    Writes the entry — the server's URL, the bearer, and the runtime
    attribution header — resolved once, now: unlike the hooks, a static entry is
    read by Claude itself, so the file holds the literal credential and rotating
    it means re-running install. Everything else in the file is kept, and
    re-running replaces the entry in place.
    """
    target = octomate_url(url)
    secret = octomate_secret()
    path = mcp_config_file(scope, file)
    document = load_settings(path)
    if scope is McpScope.local:
        container = document.projects.setdefault(str(Path.cwd()), McpContainer())
        document.model_fields_set.add("projects")
    else:
        container = document
    container.mcp_servers[OCTOMATE_SERVER_KEY] = McpServer(
        type="http",
        url=target,
        headers={
            "Authorization": f"Bearer {secret}",
            CLIENT_HEADER: CLAUDE_NATIVE_CLIENT,
        },
    )
    container.model_fields_set.add("mcp_servers")
    write_settings(path, document)
    typer.echo(f"Installed the Octomate MCP entry → {target}")
    typer.echo(f"  file:   {path}")
    if scope is McpScope.local:
        typer.echo(f"  scope:  local ({Path.cwd()})")
    typer.echo(f"  client: {CLAUDE_NATIVE_CLIENT}")
    typer.echo(
        "  auth:   embedded — the file holds the literal credential; rotation "
        "means re-running install"
    )


@mcp_typer.command("uninstall")
def mcp_uninstall(
    scope: McpScopeOption = McpScope.local, file: McpFileOption = None
) -> None:
    """Remove the Octomate MCP entry, leaving every other server and setting."""
    path = mcp_config_file(scope, file)
    document = load_settings(path)
    key = str(Path.cwd())
    container = document.projects.get(key) if scope is McpScope.local else document
    if container is None or OCTOMATE_SERVER_KEY not in container.mcp_servers:
        typer.echo(f"No Octomate MCP entry in {path}")
        raise typer.Exit()
    del container.mcp_servers[OCTOMATE_SERVER_KEY]
    if not container.mcp_servers:
        container.model_fields_set.discard("mcp_servers")
        if scope is McpScope.local and not container.model_dump(exclude_unset=True):
            del document.projects[key]
            if not document.projects:
                document.model_fields_set.discard("projects")
    write_settings(path, document)
    typer.echo(f"Removed the Octomate MCP entry from {path}")


@mcp_typer.command("show")
def mcp_show(
    scope: McpScopeOption = McpScope.local, file: McpFileOption = None
) -> None:
    """Show the Octomate MCP entry, credential masked."""
    path = mcp_config_file(scope, file)
    document = load_settings(path)
    container = (
        document.projects.get(str(Path.cwd())) if scope is McpScope.local else document
    )
    entry = (
        container.mcp_servers.get(OCTOMATE_SERVER_KEY)
        if container is not None
        else None
    )
    if entry is None:
        typer.echo(f"No Octomate MCP entry in {path}")
        raise typer.Exit()
    client = entry.headers.get(CLIENT_HEADER) if entry.headers is not None else None
    typer.echo(f"Octomate MCP entry in {path}:")
    typer.echo(f"  url:    {entry.url}")
    typer.echo(f"  client: {client}")
    typer.echo("  auth:   Bearer ***")
