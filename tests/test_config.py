from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError
from pydantic_settings import SettingsConfigDict

from octomate.config import (
    AgentModelConfig,
    ChannelsConfig,
    ClaudeCodeConfig,
    GitHubMcpConfig,
    LarkChannelConfig,
    LinearMcpConfig,
    McpConfig,
    NapcatChannelConfig,
    OctomateConfig,
    SlackChannelConfig,
)


class DefaultYamlOnlyConfig(OctomateConfig):
    model_config = SettingsConfigDict(
        env_prefix="OCTOMATE_",
        env_nested_delimiter="__",
        yaml_file=("octomate.default.yaml",),
        yaml_config_section="octomate",
        nested_model_default_partial_update=True,
        extra="ignore",
    )


def test_default_yaml_placeholders_fail_without_override() -> None:
    # octomate.default.yaml ships `~` placeholders that must be overridden in
    # octomate.yaml or via env; loading the defaults file alone fails fast with
    # clear validation errors rather than silently falling back to code defaults.
    with pytest.raises(ValidationError):
        DefaultYamlOnlyConfig()


def test_channels_default_to_none() -> None:
    channels = ChannelsConfig()
    assert channels.slack is None
    assert channels.lark is None
    assert channels.napcat is None
    assert channels.dev_ui is not None
    assert channels.dev_ui.triage == AgentModelConfig(
        model="deepseek:deepseek-v4-flash"
    )
    assert channels.dev_ui.receptions == [
        AgentModelConfig(model="deepseek:deepseek-v4-pro")
    ]


def test_channel_config_parses_supported_channels() -> None:
    config = OctomateConfig(
        channels={
            "dev_ui": None,
            "lark": None,
            "napcat": None,
            "slack": {
                "app_id": "A-test",
                "bot_token": "xoxb-test",
                "app_token": "xapp-test",
            },
            "lark": {
                "app_id": "cli-test",
                "app_secret": "secret",
            },
            "napcat": {
                "ws_url": "ws://127.0.0.1:3001",
                "http_url": "http://127.0.0.1:3000",
            },
        }
    )

    assert isinstance(config.channels.slack, SlackChannelConfig)
    assert isinstance(config.channels.lark, LarkChannelConfig)
    assert isinstance(config.channels.napcat, NapcatChannelConfig)
    assert config.channels.slack.stream.flush_interval == 0.5
    assert config.channels.lark.stream.flush_interval == 0.2
    assert config.channels.lark.stream.min_chars == 1
    assert config.channels.napcat.stream.enabled is False


def test_channel_config_parses_agent_model_routes() -> None:
    config = OctomateConfig(
        agents={"claude": {"models": ["opus"]}},
        channels={
            "dev_ui": None,
            "lark": None,
            "napcat": None,
            "slack": {
                "app_id": "A-test",
                "bot_token": "xoxb-test",
                "app_token": "xapp-test",
                "triage": {"agent": "inkling", "model": "deepseek:deepseek-v4-flash"},
                "receptions": [
                    {"agent": "inkling", "model": "deepseek:deepseek-v4-pro"},
                    {"agent": "claude", "model": "opus"},
                ],
            }
        }
    )

    assert config.channels.slack is not None
    assert config.channels.slack.triage == AgentModelConfig(
        agent="inkling", model="deepseek:deepseek-v4-flash"
    )
    assert config.channels.slack.receptions == [
        AgentModelConfig(agent="inkling", model="deepseek:deepseek-v4-pro"),
        AgentModelConfig(agent="claude", model="opus"),
    ]


def test_claude_code_config_uses_model_route_mapping() -> None:
    config = ClaudeCodeConfig.model_validate(
        {
            "models": ["opus", "sonnet"],
        }
    )

    assert config.models == {"opus", "sonnet"}
    assert not hasattr(config, "model")


def test_claude_code_config_requires_model_mapping() -> None:
    with pytest.raises(ValidationError):
        ClaudeCodeConfig.model_validate(
            {
                "models": [
                    "opus",
                    {"opus": "claude-opus-4-8"},
                ]
            }
        )


def test_claude_code_config_defaults_to_fixed_model_set() -> None:
    config = ClaudeCodeConfig()

    assert config.models == {"haiku", "sonnet[1m]", "opus[1m]", "opusplan[1m]"}


def test_claude_code_config_validates_model_names() -> None:
    with pytest.raises(ValidationError, match="Input should be"):
        ClaudeCodeConfig(models={"missing"})


def test_claude_code_config_accepts_documented_model_aliases() -> None:
    config = ClaudeCodeConfig(
        models={"best", "opus[1m]", "sonnet[1m]", "opusplan[1m]"}
    )

    assert config.models == {"best", "opus[1m]", "sonnet[1m]", "opusplan[1m]"}


def test_channel_agent_routes_must_reference_configured_agent() -> None:
    with pytest.raises(ValidationError) as exc_info:
        OctomateConfig(
            channels={
                "slack": {
                    "app_id": "A-test",
                    "bot_token": "xoxb-test",
                    "app_token": "xapp-test",
                    "triage": {
                        "agent": "ghost",
                        "model": "deepseek:deepseek-v4-flash",
                    },
                }
            }
        )

    [error] = exc_info.value.errors()
    assert error["type"] == "channel_agent_route"
    assert error["loc"] == ("channels", "slack", "triage", "agent")
    assert error["msg"] == "'ghost' does not match a configured agent tentacle"


def test_channel_agent_route_validation_reports_all_errors() -> None:
    with pytest.raises(ValidationError) as exc_info:
        OctomateConfig(
            agents={"claude": {"models": ["opus"]}},
            channels={
                "dev_ui": None,
                "napcat": None,
                "slack": {
                    "app_id": "A-test",
                    "bot_token": "xoxb-test",
                    "app_token": "xapp-test",
                    "triage": {
                        "agent": "ghost",
                        "model": "deepseek:deepseek-v4-flash",
                    },
                    "receptions": [
                        {"agent": "inkling", "model": "openai:gpt-5.2"},
                        {"agent": "claude", "model": "sonnet"},
                    ],
                },
                "lark": {
                    "enabled": False,
                    "app_id": "cli-test",
                    "app_secret": "secret",
                    "triage": {
                        "agent": "nobody",
                        "model": "deepseek:deepseek-v4-flash",
                    },
                    "receptions": [],
                },
            },
        )

    errors = {
        tuple(error["loc"]): error["msg"] for error in exc_info.value.errors()
    }
    assert errors == {
        (
            "channels",
            "slack",
            "triage",
            "agent",
        ): "'ghost' does not match a configured agent tentacle",
        (
            "channels",
            "slack",
            "receptions",
            0,
            "model",
        ): "'openai:gpt-5.2' is not configured in agents.inkling.models",
        (
            "channels",
            "slack",
            "receptions",
            1,
            "model",
        ): "'sonnet' is not configured in agents.claude.models",
        (
            "channels",
            "lark",
            "triage",
            "agent",
        ): "'nobody' does not match a configured agent tentacle",
    }


def test_disabled_channel_agent_routes_are_validated() -> None:
    with pytest.raises(ValidationError) as exc_info:
        OctomateConfig(
            channels={
                "slack": {
                    "enabled": False,
                    "app_id": "A-test",
                    "bot_token": "xoxb-test",
                    "app_token": "xapp-test",
                    "triage": {
                        "agent": "ghost",
                        "model": "deepseek:deepseek-v4-flash",
                    },
                }
            }
        )

    [error] = exc_info.value.errors()
    assert error["loc"] == ("channels", "slack", "triage", "agent")
    assert error["msg"] == "'ghost' does not match a configured agent tentacle"


def test_channel_claude_route_requires_claude_agent_config() -> None:
    with pytest.raises(ValidationError) as exc_info:
        OctomateConfig(
            agents={"claude": None},
            channels={
                "dev_ui": None,
                "lark": None,
                "napcat": None,
                "slack": {
                    "app_id": "A-test",
                    "bot_token": "xoxb-test",
                    "app_token": "xapp-test",
                    "receptions": [{"agent": "claude", "model": "opus"}],
                }
            }
        )

    [error] = exc_info.value.errors()
    assert error["loc"] == ("channels", "slack", "receptions", 0, "agent")
    assert error["msg"] == "'claude' does not match a configured agent tentacle"


def test_channel_routes_require_model() -> None:
    with pytest.raises(ValidationError) as exc_info:
        OctomateConfig(
            agents={"claude": {"models": ["opus"]}},
            channels={
                "dev_ui": None,
                "lark": None,
                "napcat": None,
                "slack": {
                    "app_id": "A-test",
                    "bot_token": "xoxb-test",
                    "app_token": "xapp-test",
                    "receptions": [{"agent": "claude"}],
                }
            },
        )
    [error] = exc_info.value.errors()
    assert error["loc"] == ("channels", "slack", "receptions", 0, "model")
    assert error["msg"] == "Field required"


def test_channel_claude_routes_must_reference_configured_model() -> None:
    with pytest.raises(ValidationError) as exc_info:
        OctomateConfig(
            agents={"claude": {"models": ["opus"]}},
            channels={
                "dev_ui": None,
                "lark": None,
                "napcat": None,
                "slack": {
                    "app_id": "A-test",
                    "bot_token": "xoxb-test",
                    "app_token": "xapp-test",
                    "receptions": [
                        {"agent": "claude", "model": "sonnet"}
                    ],
                }
            },
        )
    [error] = exc_info.value.errors()
    assert error["loc"] == ("channels", "slack", "receptions", 0, "model")
    assert error["msg"] == "'sonnet' is not configured in agents.claude.models"


def test_channel_inkling_routes_must_reference_configured_model() -> None:
    with pytest.raises(ValidationError) as exc_info:
        OctomateConfig(
            channels={
                "slack": {
                    "app_id": "A-test",
                    "bot_token": "xoxb-test",
                    "app_token": "xapp-test",
                    "receptions": [{"agent": "inkling", "model": "openai:gpt-5.2"}],
                }
            }
        )
    [error] = exc_info.value.errors()
    assert error["loc"] == ("channels", "slack", "receptions", 0, "model")
    assert error["msg"] == "'openai:gpt-5.2' is not configured in agents.inkling.models"


def test_mcp_defaults_to_no_servers() -> None:
    mcp = McpConfig()
    assert mcp.github is None
    assert mcp.linear is None


def test_mcp_config_parses_servers() -> None:
    config = OctomateConfig(
        mcp={
            "github": {"enabled": True, "token": "ghp_test", "read_only": True},
            "linear": {"enabled": True, "token": "lin_test"},
        }
    )

    assert isinstance(config.mcp.github, GitHubMcpConfig)
    assert config.mcp.github.enabled is True
    assert config.mcp.github.read_only is True
    assert config.mcp.github.token is not None
    assert config.mcp.github.token.get_secret_value() == "ghp_test"
    assert config.mcp.github.url == "https://api.githubcopilot.com/mcp/"

    assert isinstance(config.mcp.linear, LinearMcpConfig)
    assert config.mcp.linear.url == "https://mcp.linear.app/mcp"


def test_mcp_token_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OCTOMATE__MCP__GITHUB__ENABLED", "true")
    monkeypatch.setenv("OCTOMATE__MCP__GITHUB__TOKEN", "ghp_env")

    config = OctomateConfig()

    assert config.mcp.github is not None
    assert config.mcp.github.token is not None
    assert config.mcp.github.token.get_secret_value() == "ghp_env"


def test_channel_stream_config_uses_partial_defaults_from_yaml(
    tmp_path: Path,
) -> None:
    config_file = tmp_path / "octomate.yaml"
    config_file.write_text(
        """
octomate:
  channels:
    slack:
      app_id: A-test
      bot_token: xoxb-test
      app_token: xapp-test
      stream:
        enabled: false
    lark:
      app_id: cli-test
      app_secret: secret
      stream:
        enabled: false
    napcat:
      ws_url: ws://127.0.0.1:3001
      http_url: http://127.0.0.1:3000
      stream:
        flush_interval: 0.3
""",
        encoding="utf-8",
    )

    class PartialYamlConfig(OctomateConfig):
        model_config = SettingsConfigDict(
            env_prefix="OCTOMATE_",
            env_nested_delimiter="__",
            yaml_file=(config_file,),
            yaml_config_section="octomate",
            nested_model_default_partial_update=True,
            extra="ignore",
        )

    config = PartialYamlConfig()

    assert config.channels.slack is not None
    assert config.channels.slack.stream.enabled is False
    assert config.channels.slack.stream.flush_interval == 0.5

    assert config.channels.lark is not None
    assert config.channels.lark.stream.enabled is False
    assert config.channels.lark.stream.flush_interval == 0.2
    assert config.channels.lark.stream.min_chars == 1

    assert config.channels.napcat is not None
    assert config.channels.napcat.stream.enabled is False
    assert config.channels.napcat.stream.flush_interval == 0.3
