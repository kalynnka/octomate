"""GitHub's MCP server, linked through its device flow, and the scopes it takes."""

from __future__ import annotations

from typing import Literal, TypeAlias

from pydantic import Field

from octomate.config.mcp.base import OAuthMcpConfig

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


class GitHubMcpConfig(OAuthMcpConfig):
    """GitHub's account-scoped MCP server, linked through its device flow."""

    type: Literal["github"] = "github"
    url: str = "https://api.githubcopilot.com/mcp/"
    client_id: str = Field(
        description="OAuth App client id, with Device Flow enabled. Required even "
        "while `enabled` is false: a block without one describes nothing."
    )
    scopes: list[GitHubScope] = Field(
        default_factory=list,
        description="The access each user is asked for when they connect. Fixed at "
        "authorization: widening this later means every connected user reconnects.",
    )
