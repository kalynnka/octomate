from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from octomate.config.mcp import McpConfig

GITHUB_CONNECTOR_ID = "github"


class GitHubMcpConfig(McpConfig):
    """GitHub's account-scoped MCP server, reusing the shared MCP connection config."""

    url: str = "https://api.githubcopilot.com/mcp/"
    read_only: bool = False


class GitHubIntegrationConfig(BaseModel):
    """Per-user GitHub integration: device OAuth plus the account-scoped MCP server.

    Unlike a bare ``mcp`` server with one operator token, this integration produces a
    ``GitHubCapability`` that authorizes each registered user from their channel and
    mounts the GitHub MCP toolset with that user's own encrypted token.
    """

    model_config = ConfigDict(extra="ignore", hide_input_in_errors=True)

    enabled: bool = False
    id: str = Field(
        default=GITHUB_CONNECTOR_ID,
        description="Names this integration to the model: the id it loads the "
        "capability under, and the id that capability is referenced by in a run.",
    )
    client_id: str = Field(
        description="OAuth App client id, with Device Flow enabled. Required even "
        "while `enabled` is false: an integration block without one describes nothing."
    )
    scopes: list[str] = Field(default_factory=list)
    max_cached_users: int = Field(
        default=32,
        ge=1,
        description="Warm GitHub MCP sessions kept — one per connected user — before "
        "the least-recently-used session is closed.",
    )
    mcp: GitHubMcpConfig = Field(default_factory=GitHubMcpConfig)


class IntegrationsConfig(BaseModel):
    model_config = ConfigDict(extra="ignore")

    github: GitHubIntegrationConfig | None = None
