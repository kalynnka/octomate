from __future__ import annotations

from pydantic_ai.mcp import MCPToolset
from pydantic_ai.toolsets import AbstractToolset

from octomate.config import McpConfig


def build_mcp_toolsets(config: McpConfig) -> list[AbstractToolset[None]]:
    """Build deferred-loading MCP toolsets for each enabled vendor server.

    Each server is connected over Streamable HTTP (the default transport for an
    HTTP URL) with the operator token in the `Authorization` header. Tool names
    are prefixed per vendor (`github_…`, `linear_…`) so the two servers can't
    collide (e.g. both expose `list_issues`), and `.defer_loading()` keeps them
    hidden from the model until discovered through the auto-injected ToolSearch.
    """
    toolsets: list[AbstractToolset[None]] = []

    if (gh := config.github) and gh.enabled:
        if gh.token is None:
            raise ValueError("mcp.github.enabled but no token set")
        url = gh.url.rstrip("/") + "/readonly" if gh.read_only else gh.url
        toolsets.append(
            MCPToolset(
                url,
                headers={"Authorization": f"Bearer {gh.token.get_secret_value()}"},
                id="github",
            )
            .prefixed("github")
            .defer_loading()
        )

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
