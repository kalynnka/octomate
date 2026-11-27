"""Shared native MCP endpoint, attribution, and installation authentication options.

API-key entries embed the configured bearer. OAuth entries let each harness own
its authorization and token storage independently of the CLI login.
"""

from __future__ import annotations

from typing import Annotated

import typer

from octomate_cli.config import (
    CLISettings,
    cli_settings,
)

OAuthOption = Annotated[
    bool | None,
    typer.Option(
        "--oauth/--api-key",
        help="Override mcp_auth in cli.toml: native OAuth login or an embedded API key.",
    ),
]


# The served endpoint under the base URL: the host mounts its one MCP server
# under the server's name, `octomate`, and the transport answers at `/mcp` in it.
OCTOMATE_MCP_PATH = "/octomate/mcp"

# The entry name every client file mounts the server under — also dsh's
# `serverName` — so each runtime names the tools `mcp__octomate__<tool>`.
OCTOMATE_SERVER_KEY = "octomate"

# The header a session's calls attribute their configured agent with.
# Attribution within the bearer's trust
# domain, not authentication: the bearer is what authenticates.
CLIENT_HEADER = "X-Octomate-Client"


def octomate_url(url: str | None) -> str:
    """The full `/octomate/mcp` URL an install writes, from the pinned base or the
    client's own resolution — refused when nothing names an address, since a
    static entry pointing nowhere would fail every session's tool listing."""
    base = url if url is not None else cli_settings().url
    if base is None:
        raise typer.BadParameter(
            f"no --url given, {CLISettings.env('url')} is unset, and no cli.toml names "
            "a url — a static MCP entry needs a concrete address; run "
            "`octomate configure --url http://<host>:<port>`"
        )
    return base.rstrip("/") + OCTOMATE_MCP_PATH


def octomate_secret(oauth: bool | None = None) -> str | None:
    """Resolve the install's auth mode; OAuth leaves token ownership to the harness."""
    settings = cli_settings()
    use_oauth = oauth if oauth is not None else settings.mcp_auth == "oauth"
    if use_oauth:
        return None
    token = settings.token
    if token is None:
        raise typer.BadParameter(
            f"no credential resolves — {CLISettings.env('token')} is unset and no "
            "cli.toml holds one; run `octomate configure --token <api-token>` first. The entry embeds the "
            "literal credential, so installing without one would only 401."
        )
    return token
