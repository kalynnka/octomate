from __future__ import annotations

from pydantic_ai.mcp import MCPToolset
from pydantic_ai.toolsets import AbstractToolset

from octomate.config import McpServerConfig


def build_mcp_toolsets(
    servers: dict[str, McpServerConfig],
) -> list[AbstractToolset[None]]:
    """Build process-wide MCP toolsets for enabled operator credentials.

    Each server's configured key becomes its MCP session id, and the prefix its tools
    are exposed under unless the server overrides it; its warm budget rides along as the
    toolset's own ``init_timeout``, so a slow server is bounded wherever it connects.
    Loading is deferred until the model discovers the server through ToolSearch.
    """
    return [
        MCPToolset(
            server.url,
            headers={"Authorization": f"Bearer {server.token.get_secret_value()}"},
            id=key,
            init_timeout=server.warm_timeout_seconds,
        )
        .prefixed(server.prefix or key)
        .defer_loading()
        for key, server in servers.items()
        if server.enabled
    ]
