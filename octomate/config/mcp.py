from __future__ import annotations

from pydantic import BaseModel, Field, SecretStr


class McpConfig(BaseModel):
    """Connection settings for one MCP server.

    The single class every MCP config uses: a bare operator server subclasses it
    (adding its own auth/enable), and an integration holds one as its ``mcp`` attribute.
    """

    url: str
    warm_timeout_seconds: float = Field(
        default=16.0,
        gt=0,
        description="Seconds this server gets to open its MCP session before a stuck "
        "one degrades to connecting on the next run. Carried by the toolset as its "
        "`init_timeout`, and by a per-user session as its warming budget.",
    )
    prefix: str | None = Field(
        default=None,
        description="Prefix every tool of this server is exposed under. Defaults to "
        "the name the server is configured against — the `mcp:` key for a bare server, "
        "the connector id for an integration. Worth setting when one vendor is mounted "
        "twice, since two accounts on the same MCP server would otherwise offer the "
        "model two sets of identically named tools.",
    )


class McpIntegrationConfig(McpConfig):
    """The account-scoped MCP server behind a per-user OAuth integration.

    Connected with the initiating user's own token rather than an operator
    credential, so it carries no ``token``; what it adds is the one choice a
    deployment still makes about the server itself.
    """

    read_only: bool = Field(
        default=False,
        description="Mount the server's read-only endpoint variant, so a connection "
        "that could write is not asked to.",
    )


class McpServerConfig(McpConfig):
    """A bare vendor MCP server, connected process-wide with one operator credential.

    Configured under ``mcp`` against a key of the deployment's choosing: that key is the
    id its MCP session carries and the prefix its tools are exposed under. Per-user OAuth
    integrations (GitHub) live under ``integrations`` instead.
    """

    enabled: bool = True
    token: SecretStr
