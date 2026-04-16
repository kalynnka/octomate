from __future__ import annotations

from pydantic import SecretStr
from pydantic_ai.mcp import MCPServerStreamableHTTP
from pydantic_settings import BaseSettings, SettingsConfigDict, YamlConfigSettingsSource

from octomate.tentacles.agent.skills import SkillManager, ToolPermission

REMOTE_MCP_URL = "https://mcp.linear.app/mcp"


class LinearConfig(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="LINEAR_",
        yaml_file=["octomate.default.yaml", "octomate.yaml"],
        yaml_config_section="linear",
    )

    api_key: SecretStr = SecretStr("")
    tools: dict[str, ToolPermission] = {
        "search_issues": "bypass",
        "get_issue": "bypass",
        "create_issue": "default",
        "update_issue": "default",
        "list_projects": "bypass",
        "create_comment": "default",
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


def register(manager: SkillManager) -> None:
    config = LinearConfig()
    api_key = config.api_key.get_secret_value()
    if not api_key:
        return

    mcp_server = MCPServerStreamableHTTP(
        url=REMOTE_MCP_URL,
        headers={"Authorization": f"Bearer {api_key}"},
    )

    manager.register_mcp(
        name="linear",
        description=(
            "Operates on Linear on behalf of the owner. "
            "Load this skill when the user wants to search, read, create, "
            "or update Linear issues, or browse Linear projects. "
            "Do NOT load for tasks unrelated to Linear."
        ),
        toolset=mcp_server,
        approvers=config.approvers or None,
        tool_permissions=config.tools or None,
    )
