"""The gateway's static MCP client config — the three facts every `octomate
<runtime> mcp install` writes: where the gateway is, the credential, and which
runtime the calls are from.

Owned here so the runtimes' installers cannot drift apart. The server holds the
same literals (`octomate.mcp.gateway`, `octomate.types.threads`); the CLI cannot
import that half, so the tests are what hold the two together. Unlike the hook
commands — whose scripts resolve the address and credential from the environment
when each hook fires — a static MCP entry is read by the runtime itself, so both
are resolved once, at install time, and written into the file: the file holds
the literal credential, and rotating it means re-running install.
"""

from __future__ import annotations

import typer

from octomate_cli.config import (
    CLISettings,
    cli_settings,
)

# The served gateway's endpoint under the base URL: the server mounts each MCP
# server at `/<name>` + its `mcp_path`, and the gateway's name is `gateway`.
GATEWAY_MCP_PATH = "/gateway/mcp"

# The entry name every client file mounts the server under — also dsh's
# `serverName` — so each runtime names the tools `mcp__gateway__<spell>`.
GATEWAY_SERVER_KEY = "gateway"

# The header a native session's calls attribute their runtime with, and the
# value each runtime's install writes. Attribution within the bearer's trust
# domain, not authentication: the bearer is what authenticates.
CLIENT_HEADER = "X-Octomate-Client"
CLAUDE_NATIVE_CLIENT = "claude-native"
CODEX_NATIVE_CLIENT = "codex-native"
DEEPSEEK_NATIVE_CLIENT = "deepseek-native"


def gateway_url(url: str | None) -> str:
    """The full `/gateway/mcp` URL an install writes, from the pinned base or the
    client's own resolution — refused when nothing names an address, since a
    static entry pointing nowhere would fail every session's tool listing."""
    base = url if url is not None else cli_settings().url
    if base is None:
        raise typer.BadParameter(
            f"no --url given, {CLISettings.env('url')} is unset, and no cli.toml names "
            "a url — a static MCP entry needs a concrete address; run "
            "`octomate configure --url http://<host>:<port>`"
        )
    return base.rstrip("/") + GATEWAY_MCP_PATH


def gateway_secret() -> str:
    """The credential an install embeds in the entry's Authorization header —
    refused when nothing resolves, since the entry would 401 on every call."""
    secret = cli_settings().secret
    if secret is None:
        raise typer.BadParameter(
            f"no credential resolves — {CLISettings.env('secret')} is unset and no "
            "cli.toml holds one; run `octomate configure` first. The entry embeds the "
            "literal credential, so installing without one would only 401."
        )
    return secret
