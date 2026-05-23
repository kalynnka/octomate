from __future__ import annotations

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
