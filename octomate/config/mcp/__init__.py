"""Named MCP tentacles using operator tokens or per-user OAuth."""

from typing import Annotated

from pydantic import Field

from octomate.config.mcp.base import (
    BareMcpConfig,
    DiscoveredOAuthMcpConfig,
    McpConfig,
    OAuthMcpConfig,
)

type McpConfigVariant = Annotated[
    BareMcpConfig | OAuthMcpConfig | DiscoveredOAuthMcpConfig,
    Field(discriminator="type"),
]

__all__ = [
    "BareMcpConfig",
    "DiscoveredOAuthMcpConfig",
    "McpConfig",
    "McpConfigVariant",
    "OAuthMcpConfig",
]
