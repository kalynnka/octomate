"""Linear's MCP server, linked through its authorization-code flow, and the scopes
it takes."""

from __future__ import annotations

from typing import Literal, TypeAlias

from pydantic import AnyHttpUrl, Field, SecretStr

from octomate.config.mcp.base import OAuthMcpConfig

# Every scope Linear documents for an OAuth application. A literal for the same
# reason `GitHubScope` is one: a misspelled scope is otherwise only discovered at
# the consent screen, where the user grants whatever Linear did recognise.
LinearScope: TypeAlias = Literal[
    "read",
    "write",
    "issues:create",
    "comments:create",
    "timeSchedule:write",
    "admin",
]


class LinearMcpConfig(OAuthMcpConfig):
    """Linear's account-scoped MCP server, linked through its authorization-code
    flow."""

    type: Literal["linear"] = "linear"
    url: str = "https://mcp.linear.app/mcp"
    client_id: str = Field(
        description="OAuth application client id, from Linear's workspace settings. "
        "Required even while `enabled` is false: a block without one describes nothing."
    )
    client_secret: SecretStr | None = Field(
        default=None,
        description="OAuth application client secret. Optional — Linear accepts PKCE "
        "without one, and this deployment holds the verifier either way. Prefer the "
        "environment over YAML.",
    )
    callback_base_uri: AnyHttpUrl = Field(
        default=AnyHttpUrl("http://localhost:8000"),
        description="Where the authorizing BROWSER reaches this deployment — not "
        "where the internet does, since Linear only redirects the user agent and "
        "never connects here. `<this>/oauth/<key>/callback` is the redirect URL the "
        "Linear application must register, character for character.",
    )
    scopes: list[LinearScope] = Field(
        default_factory=lambda: ["read", "write"],
        description="The access each user is asked for when they connect. Fixed at "
        "authorization: widening this later means every connected user reconnects.",
    )
