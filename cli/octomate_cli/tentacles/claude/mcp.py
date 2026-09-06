"""Install and manage the claude tentacle's gateway MCP configuration."""

from __future__ import annotations

from enum import Enum
from pathlib import Path
from typing import Annotated

import typer

from octomate_cli.config import CLISettings
from octomate_cli.tentacles.claude.config import load_settings, write_settings
from octomate_cli.tentacles.mcp import (
    CLAUDE_NATIVE_CLIENT,
    CLIENT_HEADER,
    GATEWAY_SERVER_KEY,
    gateway_secret,
    gateway_url,
)

mcp_typer = typer.Typer(
    help="Manage the gateway MCP entry for native Claude Code sessions.",
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
    """Point native Claude Code sessions at the served gateway.

    Writes the gateway entry — the gateway's URL, the bearer, and the runtime
    attribution header — resolved once, now: unlike the hooks, a static entry is
    read by Claude itself, so the file holds the literal credential and rotating
    it means re-running install. Everything else in the file is kept, and
    re-running replaces the entry in place.
    """
    target = gateway_url(url)
    secret = gateway_secret()
    path = mcp_config_file(scope, file)
    document = load_settings(path)
    if scope is McpScope.local:
        projects = document.setdefault("projects", {})
        if not isinstance(projects, dict):
            raise typer.BadParameter(f"{path} has a non-object 'projects' section")
        container = projects.setdefault(str(Path.cwd()), {})
        if not isinstance(container, dict):
            raise typer.BadParameter(
                f"{path} has a non-object entry for project {Path.cwd()}"
            )
    else:
        container = document
    servers = container.setdefault("mcpServers", {})
    if not isinstance(servers, dict):
        raise typer.BadParameter(f"{path} has a non-object 'mcpServers' section")
    servers[GATEWAY_SERVER_KEY] = {
        "type": "http",
        "url": target,
        "headers": {
            "Authorization": f"Bearer {secret}",
            CLIENT_HEADER: CLAUDE_NATIVE_CLIENT,
        },
    }
    write_settings(path, document)
    typer.echo(f"Installed the gateway MCP entry → {target}")
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
    """Remove the gateway MCP entry, leaving every other server and setting."""
    path = mcp_config_file(scope, file)
    document = load_settings(path)
    key = str(Path.cwd())
    projects = document.get("projects")
    if scope is McpScope.local:
        entry = projects.get(key) if isinstance(projects, dict) else None
        container = entry if isinstance(entry, dict) else None
    else:
        container = document
    servers = container.get("mcpServers") if container is not None else None
    if not isinstance(servers, dict) or GATEWAY_SERVER_KEY not in servers:
        typer.echo(f"No gateway MCP entry in {path}")
        raise typer.Exit()
    del servers[GATEWAY_SERVER_KEY]
    if not servers and container is not None:
        del container["mcpServers"]
        # A local install into a fresh file created the project entry too; an
        # entry emptied by this removal goes with it, a lived-in one stays.
        if scope is McpScope.local and not container and isinstance(projects, dict):
            del projects[key]
            if not projects:
                del document["projects"]
    write_settings(path, document)
    typer.echo(f"Removed the gateway MCP entry from {path}")


@mcp_typer.command("show")
def mcp_show(
    scope: McpScopeOption = McpScope.local, file: McpFileOption = None
) -> None:
    """Show the gateway MCP entry, credential masked."""
    path = mcp_config_file(scope, file)
    document = load_settings(path)
    if scope is McpScope.local:
        projects = document.get("projects")
        container = (
            projects.get(str(Path.cwd())) if isinstance(projects, dict) else None
        )
    else:
        container = document
    servers = container.get("mcpServers") if isinstance(container, dict) else None
    entry = servers.get(GATEWAY_SERVER_KEY) if isinstance(servers, dict) else None
    if not isinstance(entry, dict):
        typer.echo(f"No gateway MCP entry in {path}")
        raise typer.Exit()
    headers = entry.get("headers")
    client = headers.get(CLIENT_HEADER) if isinstance(headers, dict) else None
    typer.echo(f"Gateway MCP entry in {path}:")
    typer.echo(f"  url:    {entry.get('url')}")
    typer.echo(f"  client: {client}")
    typer.echo("  auth:   Bearer ***")
