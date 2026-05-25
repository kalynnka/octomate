from __future__ import annotations

from pathlib import Path

from pydantic_settings import SettingsConfigDict

from octomate.config import (
    ChannelsConfig,
    LarkChannelConfig,
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


def test_default_config_loads_yaml_section() -> None:
    config = DefaultYamlOnlyConfig()
    assert config.agents.inkling.model == "gemini-3-flash-preview"
    assert isinstance(config.channels, ChannelsConfig)
    assert config.channels.slack is None
    assert config.channels.lark is None
    assert config.channels.napcat is None


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
