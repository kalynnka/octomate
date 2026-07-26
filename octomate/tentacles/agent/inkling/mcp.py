from __future__ import annotations

from pydantic import SecretStr
from pydantic_ai.mcp import MCPToolset
from pydantic_ai.toolsets import AbstractToolset

from octomate.config import GitHubMcpConfig, McpConfig


def build_github_mcp_toolset(
    config: GitHubMcpConfig,
    access_token: SecretStr,
) -> AbstractToolset[None]:
    """Build one run-scoped GitHub MCP toolset for its owning user."""
    url = config.url.rstrip("/") + "/readonly" if config.read_only else config.url
    return (
        MCPToolset(
            url,
            headers={"Authorization": f"Bearer {access_token.get_secret_value()}"},
            id="github",
        )
        .prefixed("github")
        .defer_loading()
    )


def build_mcp_toolsets(config: McpConfig) -> list[AbstractToolset[None]]:
    """Build process-wide MCP toolsets for enabled operator credentials.

    GitHub is deliberately absent: Inkling mounts it per run from the triggering
    user's OAuth connection. Linear still uses its configured operator token. Each
    server is prefixed and deferred until discovered through ToolSearch.
    """
    toolsets: list[AbstractToolset[None]] = []

    if (lin := config.linear) and lin.enabled:
        if lin.token is None:
            raise ValueError("mcp.linear.enabled but no token set")
        toolsets.append(
            MCPToolset(
                lin.url,
                headers={"Authorization": f"Bearer {lin.token.get_secret_value()}"},
                id="linear",
            )
            .prefixed("linear")
            .defer_loading()
        )

    return toolsets
