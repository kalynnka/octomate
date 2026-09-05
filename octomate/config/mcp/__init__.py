"""MCP tentacles: a provider's server, proxied on Octomate's own as the caller.

Configured under ``mcp`` against a key of the deployment's choosing, which is the
tentacle id throughout: the prefix its tools carry unless ``prefix`` names another
and, for a linked account, the connector its tokens live under. One vendor can
therefore be mounted more than once, a key per account. ``type`` selects the
tentacle that builds it, as it does for a channel.

One module per provider a person links an account with, carrying its config and
the scopes it documents, over the shapes every MCP tentacle shares in `base`.
"""

from __future__ import annotations

from typing import Annotated, TypeAlias

from pydantic import Field

from octomate.config.mcp.base import BareMcpConfig, McpConfig, OAuthMcpConfig
from octomate.config.mcp.github import GitHubMcpConfig
from octomate.config.mcp.linear import LinearMcpConfig

McpConfigVariant: TypeAlias = Annotated[
    BareMcpConfig | GitHubMcpConfig | LinearMcpConfig,
    Field(discriminator="type"),
]
"""One configured MCP tentacle, resolved from its `type` to the tentacle that builds
it. A new provider is a module here, a variant in this union and a branch in
`build_mcp`; no other module learns its name."""

__all__ = [
    "BareMcpConfig",
    "GitHubMcpConfig",
    "LinearMcpConfig",
    "McpConfig",
    "McpConfigVariant",
    "OAuthMcpConfig",
]
