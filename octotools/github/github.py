from __future__ import annotations

from typing import Literal

from pydantic import SecretStr
from pydantic_ai.mcp import MCPServerStreamableHTTP
from pydantic_settings import BaseSettings, SettingsConfigDict, YamlConfigSettingsSource

from octomate.tentacles.agent.skills import SkillManager, ToolPermission

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
        yaml_file=["octotools/github/config.yaml"],
    )

    api_key: SecretStr = SecretStr("")
    toolsets: list[GitHubToolset] = [
        "repos",
        "issues",
        "pull_requests",
        "copilot",
        "projects",
        "context",
    ]
    tools: dict[str, dict[str, ToolPermission]] = {
        "repos": {
            "get_repository": "bypass",
            "get_file_contents": "bypass",
            "list_branches": "bypass",
        },
        "issues": {
            "create_issue": "default",
            "get_issue": "bypass",
            "list_issues": "bypass",
            "update_issue": "default",
            "search_issues": "bypass",
            "add_issue_comment": "default",
        },
        "pull_requests": {
            "create_pull_request": "default",
            "get_pull_request": "bypass",
            "list_pull_requests": "bypass",
            "update_pull_request": "default",
            "request_copilot_review": "default",
            "merge_pull_request": "default",
        },
        "copilot": {
            "create_copilot_task": "default",
        },
        "projects": {
            "get_project": "bypass",
            "add_project_item": "default",
            "list_project_items": "bypass",
            "update_project_item": "default",
        },
        "search": {
            "search_code": "bypass",
            "search_repositories": "bypass",
        },
    }
    approvers: dict[str, list[str]] = {}

    @classmethod
    def settings_customise_sources(cls, settings_cls, **kwargs):
        return (
            kwargs["init_settings"],
            kwargs["env_settings"],
            YamlConfigSettingsSource(settings_cls),
            kwargs["file_secret_settings"],
        )


CATEGORY_DESCRIPTIONS: dict[str, str] = {
    "repos": "Read GitHub repository info, file contents, and branches.",
    "issues": "Create, search, read, and update GitHub issues.",
    "pull_requests": "Create, read, update, and manage GitHub pull requests and reviews.",
    "copilot": "Trigger GitHub Copilot coding agent to work on issues.",
    "projects": "Manage GitHub Projects boards and items.",
    "search": "Search code and repositories on GitHub.",
}


def register(manager: SkillManager) -> None:
    config = GitHubConfig()
    token = config.api_key.get_secret_value()
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

    categories: dict[str, tuple[str, dict[str, ToolPermission]]] = {}
    for category, perms in config.tools.items():
        description = CATEGORY_DESCRIPTIONS.get(
            category, f"GitHub {category} operations."
        )
        categories[category] = (description, perms)

    manager.register_mcp_group(
        prefix="github",
        toolset=mcp_server,
        categories=categories,
        approvers=config.approvers or None,
    )
