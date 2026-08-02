from __future__ import annotations

from typing import Annotated, Literal, TypeAlias

from pydantic import AnyHttpUrl, BaseModel, ConfigDict, Field, SecretStr

from octomate.config.mcp import McpIntegrationConfig

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


class GitHubMcpConfig(McpIntegrationConfig):
    """GitHub's account-scoped MCP server, reusing the shared MCP connection config."""

    url: str = "https://api.githubcopilot.com/mcp/"


class LinearMcpConfig(McpIntegrationConfig):
    """Linear's account-scoped MCP server, reusing the shared MCP connection config."""

    url: str = "https://mcp.linear.app/mcp"


class OAuthIntegrationConfig(BaseModel):
    """What every per-user OAuth integration is configured with.

    Unlike a bare ``mcp`` server with one operator token, an integration authorizes
    each registered user from their channel and mounts that vendor's MCP server with
    that user's own encrypted token. What varies below this is the flow: which
    credentials the provider takes, which scopes it names, and whether a browser has
    to come back anywhere.

    Configured under ``integrations`` against a key of the deployment's choosing.
    That key is the connector id — so it keys stored connections, names the
    capability the model loads, and, unless ``mcp.prefix`` says otherwise, prefixes
    the tools. One vendor can therefore be mounted more than once, a key per account.
    """

    model_config = ConfigDict(extra="ignore", hide_input_in_errors=True)

    enabled: bool = False
    client_id: str = Field(
        description="The OAuth application's client id. Required even while `enabled` "
        "is false: an integration block without one describes nothing."
    )
    max_cached_users: int = Field(
        default=32,
        ge=1,
        description="Warm MCP sessions kept — one per connected user — before the "
        "least-recently-used session is closed.",
    )
    # `mcp` is deliberately each integration's own, not declared here: the field's
    # type is what supplies its server URL, so widening it to the shared class would
    # make a deployment spell the URL out to override anything else in the block.


class GitHubIntegrationConfig(OAuthIntegrationConfig):
    """Per-user GitHub integration: device OAuth plus the account-scoped MCP server."""

    type: Literal["github"] = "github"
    client_id: str = Field(
        description="OAuth App client id, with Device Flow enabled. Required even "
        "while `enabled` is false: an integration block without one describes nothing."
    )
    scopes: list[GitHubScope] = Field(
        default_factory=list,
        description="The access each user is asked for when they connect. Fixed at "
        "authorization: widening this later means every connected user reconnects.",
    )
    mcp: GitHubMcpConfig = Field(default_factory=GitHubMcpConfig)


class LinearIntegrationConfig(OAuthIntegrationConfig):
    """Per-user Linear integration: authorization-code OAuth plus its MCP server."""

    type: Literal["linear"] = "linear"
    client_id: str = Field(
        description="OAuth application client id, from Linear's workspace settings. "
        "Required even while `enabled` is false: an integration block without one "
        "describes nothing."
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
    mcp: LinearMcpConfig = Field(default_factory=LinearMcpConfig)


IntegrationConfig: TypeAlias = Annotated[
    GitHubIntegrationConfig | LinearIntegrationConfig,
    Field(discriminator="type"),
]
"""One configured integration, resolved from its `type` to the provider that builds
it. A new provider is a variant here plus a branch in bootstrap's composition; no
other module learns its name."""
