from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError
from pydantic_settings import SettingsConfigDict

from octomate.config import (
    ChannelsConfig,
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


def test_channel_config_parses_supported_channels() -> None:
    config = OctomateConfig(
        channels={
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
    assert config.channels.slack.stream.flush_interval == 0
    assert config.channels.lark.stream.flush_interval == 0.2
    assert config.channels.lark.stream.min_chars == 1
    assert config.channels.napcat.stream.enabled is False


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
    assert config.channels.slack.stream.flush_interval == 0

    assert config.channels.lark is not None
    assert config.channels.lark.stream.enabled is False
    assert config.channels.lark.stream.flush_interval == 0.2
    assert config.channels.lark.stream.min_chars == 1

    assert config.channels.napcat is not None
    assert config.channels.napcat.stream.enabled is False
    assert config.channels.napcat.stream.flush_interval == 0.3
