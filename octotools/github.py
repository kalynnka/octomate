from __future__ import annotations

from typing import Literal

from pydantic import SecretStr
from pydantic_ai.mcp import MCPServerStreamableHTTP
from pydantic_settings import BaseSettings, SettingsConfigDict, YamlConfigSettingsSource

from octomate.agents.manager import SkillManager

REMOTE_MCP_URL = "https://api.githubcopilot.com/mcp/"

GitHubToolset = Literal[
    "all",
    "default",
    "context",
    "actions",
    "code_security",
    "copilot",
    "copilot_spaces",
    "dependabot",
    "discussions",
    "gists",
    "git",
    "issues",
    "labels",
    "notifications",
    "orgs",
    "projects",
    "pull_requests",
    "repos",
    "secret_protection",
    "security_advisories",
    "stargazers",
    "users",
    "github_support_docs_search",
]


class GitHubConfig(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="GITHUB_",
        yaml_file=["octomate.default.yaml", "octomate.yaml"],
        yaml_config_section="github",
    )

    token: SecretStr = SecretStr("")
    toolsets: list[GitHubToolset] = ["default"]

    @classmethod
    def settings_customise_sources(cls, settings_cls, **kwargs):
        return (
            kwargs["init_settings"],
            kwargs["env_settings"],
            YamlConfigSettingsSource(settings_cls),
            kwargs["file_secret_settings"],
        )


def register(manager: SkillManager) -> None:
    config = GitHubConfig()
    token = config.token.get_secret_value()
    if not token:
        return

    headers: dict[str, str] = {"Authorization": f"Bearer {token}"}
    toolsets_value = ",".join(config.toolsets)
    if toolsets_value != "default":
        headers["X-MCP-Toolsets"] = toolsets_value

    mcp_server = MCPServerStreamableHTTP(
        url=REMOTE_MCP_URL,
        headers=headers,
    )

    manager.register_mcp(
        name="github",
        description=(
            "Operates on GitHub or Copilot on behalf of the owner. "
            "Only load and use this skill when it is confirmed to be necessary, "
            "such as when the user explicitly requests GitHub-related information, "
            "or using GitHub actions, creating/managing issues, pull requests, "
            "reading repository files, checking CI/CD workflows, or browsing "
            "code on GitHub. Do NOT load for general unrelated questions or tasks unrelated to GitHub."
        ),
        toolset=mcp_server,
    )
