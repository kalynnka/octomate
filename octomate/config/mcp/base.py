"""The shapes every MCP tentacle's config shares: the server and the name its
tools are listed under, with either one operator credential or a person's own
linked account behind it."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, SecretStr


class McpConfig(BaseModel):
    """What every MCP tentacle is configured with: the server, and the name its
    tools are listed under."""

    type: str
    enabled: bool = True
    url: str
    prefix: str | None = Field(
        default=None,
        description="Prefix every tool of this server is exposed under. Defaults to "
        "the key the server is configured against. Worth setting when one vendor is "
        "mounted twice, since two accounts on the same MCP server would otherwise "
        "offer the model two sets of identically named tools.",
    )


class BareMcpConfig(McpConfig):
    """A vendor's MCP server spoken to with one operator credential for every
    caller: the deployment's identity, not the person's."""

    type: Literal["bare"] = "bare"
    token: SecretStr


class OAuthMcpConfig(McpConfig):
    """A provider whose tools act as the person who drove the turn, with the account
    they linked under this key through the provider's own OAuth flow.

    What varies below this is the flow: which credentials the provider takes, which
    scopes it names, and whether a browser has to come back anywhere. Enabling one
    requires ``oauth.encryption_key``, since the tokens are stored.
    """

    client_id: str = Field(
        description="The OAuth application's client id. Required even while `enabled` "
        "is false: a block without one describes nothing."
    )
    read_only: bool = Field(
        default=False,
        description="Mount the server's read-only endpoint variant, so a connection "
        "that could write is not asked to.",
    )

    @property
    def endpoint(self) -> str:
        """The URL a connection speaks to: the read-only variant when asked for."""
        return self.url.rstrip("/") + "/readonly" if self.read_only else self.url
