from __future__ import annotations

from typing import Literal, TypeAlias

from pydantic import BaseModel, ConfigDict, Field

from octomate.config.mcp import McpConfig

GITHUB_CONNECTOR_ID = "github"

# Every scope GitHub documents for an OAuth App, in the order its table lists
# them (parents first, then what each includes). Spelling one wrong is otherwise
# only discovered at the authorization itself, where GitHub ignores what it does
# not recognise and hands back a token quietly missing that access; as a literal
# it fails at startup instead.
GitHubScope: TypeAlias = Literal[
    "site_admin",
    "repo",
    "repo:status",
    "repo_deployment",
    "public_repo",
    "repo:invite",
    "security_events",
    "admin:repo_hook",
    "write:repo_hook",
    "read:repo_hook",
    "admin:org",
    "write:org",
    "read:org",
    "admin:public_key",
    "write:public_key",
    "read:public_key",
    "admin:org_hook",
    "gist",
    "notifications",
    "user",
    "read:user",
    "user:email",
    "user:follow",
    "project",
    "read:project",
    "delete_repo",
    "write:packages",
    "read:packages",
    "delete:packages",
    "admin:gpg_key",
    "write:gpg_key",
    "read:gpg_key",
    "codespace",
    "workflow",
    "admin:enterprise",
    "manage_runners:enterprise",
    "manage_billing:enterprise",
    "read:enterprise",
    "read:audit_log",
]


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
    scopes: list[GitHubScope] = Field(
        default_factory=list,
        description="The access each user is asked for when they connect. Fixed at "
        "authorization: widening this later means every connected user reconnects.",
    )
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
