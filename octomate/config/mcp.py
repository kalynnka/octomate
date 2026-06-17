from __future__ import annotations

from pydantic import BaseModel, ConfigDict, SecretStr


class GitHubMcpConfig(BaseModel):
    enabled: bool = False
    token: SecretStr | None = None
    url: str = "https://api.githubcopilot.com/mcp/"
    read_only: bool = False


class LinearMcpConfig(BaseModel):
    enabled: bool = False
    token: SecretStr | None = None
    url: str = "https://mcp.linear.app/mcp"


class McpConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    github: GitHubMcpConfig | None = None
    linear: LinearMcpConfig | None = None
