"""Install and manage the codex tentacle's MCP configuration."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import tomlkit
import typer
from tomlkit.items import Table

from octomate_cli.config import CLISettings
from octomate_cli.tentacles.mcp import (
    CLIENT_HEADER,
    CODEX_NATIVE_CLIENT,
    OCTOMATE_SERVER_KEY,
    octomate_secret,
    octomate_url,
)

mcp_typer = typer.Typer(
    help="Manage the Octomate MCP entry for native Codex sessions.",
    no_args_is_help=True,
)


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
            f"${CLISettings.env('url')}, then cli.toml."
        ),
    ] = None,
    config_file: McpConfigOption = None,
) -> None:
    """Point native Codex sessions at the served MCP server.

    Writes the `mcp_servers.octomate` table — the server's URL, the bearer, and
    the runtime attribution header — preserving the operator's comments and every
    other table. The credential is embedded, as it is for the other runtimes:
    naming an environment variable instead would put one person's bearer in every
    process launched from that shell, this deployment's Codex app-servers
    included, where each turn's own kicker is the only identity a spell may run
    as. Rotating means re-running install. A driven turn pins this entry by name
    for the length of its process, so nothing here reaches it.
    """
    target = octomate_url(url)
    secret = octomate_secret()
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
    headers = tomlkit.inline_table()
    headers["Authorization"] = f"Bearer {secret}"
    headers[CLIENT_HEADER] = CODEX_NATIVE_CLIENT
    entry["http_headers"] = headers
    servers[OCTOMATE_SERVER_KEY] = entry
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(tomlkit.dumps(document))
    typer.echo(f"Installed the Octomate MCP entry → {target}")
    typer.echo(f"  file:   {path}")
    typer.echo(f"  client: {CODEX_NATIVE_CLIENT}")
    typer.echo(
        "  auth:   embedded — the file holds the literal credential; rotation "
        "means re-running install"
    )


@mcp_typer.command("uninstall")
def mcp_uninstall(config_file: McpConfigOption = None) -> None:
    """Remove the Octomate MCP entry, leaving the operator's comments and every
    other server."""
    path = mcp_config_file(config_file)
    document = load_toml(path)
    servers = document.get("mcp_servers")
    if not isinstance(servers, Table) or OCTOMATE_SERVER_KEY not in servers:
        typer.echo(f"No Octomate MCP entry in {path}")
        raise typer.Exit()
    del servers[OCTOMATE_SERVER_KEY]
    if len(servers) == 0:
        del document["mcp_servers"]
    path.write_text(tomlkit.dumps(document))
    typer.echo(f"Removed the Octomate MCP entry from {path}")


@mcp_typer.command("show")
def mcp_show(config_file: McpConfigOption = None) -> None:
    """Show the Octomate MCP entry, credential masked."""
    path = mcp_config_file(config_file)
    servers = load_toml(path).get("mcp_servers")
    entry = servers.get(OCTOMATE_SERVER_KEY) if isinstance(servers, Table) else None
    if not isinstance(entry, Table):
        typer.echo(f"No Octomate MCP entry in {path}")
        raise typer.Exit()
    headers = entry.get("http_headers")
    client = headers.get(CLIENT_HEADER) if isinstance(headers, dict) else None
    typer.echo(f"Octomate MCP entry in {path}:")
    typer.echo(f"  url:    {entry.get('url')}")
    typer.echo(f"  client: {client}")
    typer.echo("  auth:   Bearer ***")
