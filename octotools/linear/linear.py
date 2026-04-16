from __future__ import annotations

from pydantic import SecretStr
from pydantic_ai.mcp import MCPServerStreamableHTTP
from pydantic_settings import BaseSettings, SettingsConfigDict, YamlConfigSettingsSource

from octomate.tentacles.agent.skills import SkillManager, ToolPermission

REMOTE_MCP_URL = "https://mcp.linear.app/mcp"


class LinearConfig(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="LINEAR_",
        yaml_file=["octotools/linear/config.yaml"],
    )

    api_key: SecretStr = SecretStr("")
    tools: dict[str, dict[str, ToolPermission]] = {
        "issues": {
            "search_issues": "bypass",
            "list_issues": "bypass",
            "list_my_issues": "bypass",
            "get_issue": "bypass",
            "list_issue_statuses": "bypass",
            "get_issue_status": "bypass",
            "list_issue_labels": "bypass",
            "create_issue": "default",
            "update_issue": "default",
        },
        "projects": {
            "list_projects": "bypass",
            "get_project": "bypass",
            "create_project": "default",
            "update_project": "default",
        },
        "comments": {
            "list_comments": "bypass",
            "create_comment": "default",
        },
        "teams": {
            "list_teams": "bypass",
            "get_team": "bypass",
        },
        "users": {
            "list_users": "bypass",
            "get_user": "bypass",
        },
        "documents": {
            "list_documents": "bypass",
            "get_document": "bypass",
            "search_documentation": "bypass",
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
    "issues": "Search, read, create, and update Linear issues and their statuses/labels.",
    "projects": "Browse, create, and update Linear projects.",
    "comments": "Read and post comments on Linear issues.",
    "teams": "Browse Linear teams.",
    "users": "Browse Linear workspace members.",
    "documents": "Browse and search Linear documents.",
}


def register(manager: SkillManager) -> None:
    config = LinearConfig()
    api_key = config.api_key.get_secret_value()
    if not api_key:
        return

    mcp_server = MCPServerStreamableHTTP(
        url=REMOTE_MCP_URL,
        headers={"Authorization": f"Bearer {api_key}"},
    )

    categories: dict[str, tuple[str, dict[str, ToolPermission]]] = {}
    for category, perms in config.tools.items():
        description = CATEGORY_DESCRIPTIONS.get(category, f"Linear {category} operations.")
        categories[category] = (description, perms)

    manager.register_mcp_group(
        prefix="linear",
        toolset=mcp_server,
        categories=categories,
        approvers=config.approvers or None,
    )
