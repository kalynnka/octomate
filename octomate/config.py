from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, SecretStr, model_validator
from pydantic_settings import (
    BaseSettings,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
    YamlConfigSettingsSource,
)


class InklingConfig(BaseModel):
    model: str = "gemini-3-flash-preview"


class AgentsConfig(BaseModel):
    inkling: InklingConfig = Field(default_factory=InklingConfig)


class ChannelConfig(BaseModel):
    type: str
    agent_id: str = "inkling"
    mention_only: bool = True
    enabled: bool = True


class SlackChannelConfig(ChannelConfig):
    type: Literal["slack"] = "slack"
    bot_token: SecretStr
    app_token: SecretStr


class LarkChannelConfig(ChannelConfig):
    type: Literal["lark"] = "lark"
    app_id: str
    app_secret: SecretStr


class NapcatChannelConfig(ChannelConfig):
    type: Literal["napcat"] = "napcat"
    ws_url: str
    http_url: str
    access_token: SecretStr | None = None
    backoff_base: float = 1.0
    backoff_max: float = 60.0
    backoff_factor: float = 2.0


class ChannelsConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    slack: SlackChannelConfig | None = None
    lark: LarkChannelConfig | None = None
    napcat: NapcatChannelConfig | None = None


class OctomateConfig(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="OCTOMATE_",
        env_nested_delimiter="__",
        yaml_file=("octomate.default.yaml", "octomate.yaml"),
        yaml_config_section="octomate",
        extra="ignore",
    )

    agents: AgentsConfig = Field(default_factory=AgentsConfig)
    channels: ChannelsConfig = Field(default_factory=ChannelsConfig)

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        yaml_settings = YamlConfigSettingsSource(settings_cls)
        return (
            init_settings,
            env_settings,
            yaml_settings,
            dotenv_settings,
            file_secret_settings,
        )
