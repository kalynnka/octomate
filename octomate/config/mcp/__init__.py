"""Named MCP tentacles using operator tokens or configured OAuth applications."""

from typing import Annotated

from pydantic import Field

from octomate.config.mcp.base import BareMcpConfig, McpConfig, OAuthMcpConfig

type McpConfigVariant = Annotated[
    BareMcpConfig | OAuthMcpConfig, Field(discriminator="type")
]

__all__ = ["BareMcpConfig", "McpConfig", "McpConfigVariant", "OAuthMcpConfig"]
