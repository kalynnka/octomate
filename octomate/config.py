from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator
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


LogLevel = Literal["CRITICAL", "ERROR", "WARNING", "INFO", "DEBUG", "NOTSET"]


class LoggingConfig(BaseModel):
    level: LogLevel = "INFO"
    format: str = "%(asctime)s %(levelname)s [%(name)s] %(message)s"

    @field_validator("level", mode="before")
    @classmethod
    def normalize_level(cls, value: str) -> str:
        return value.upper()


class LogfireConfig(BaseModel):
    service_name: str = "octomate"
    environment: str = "development"
    send_to_logfire: bool = False
    console: bool = False
    scrub: bool = True


class ChannelStreamConfig(BaseModel):
    enabled: bool = False
    flush_interval: float = 0.5
    min_chars: int = 120
    max_chars: int = 1000
    fold_threshold: int = 1500


class SlackStreamConfig(ChannelStreamConfig):
    enabled: bool = True
    flush_interval: float = 0.0


class LarkStreamConfig(ChannelStreamConfig):
    enabled: bool = True
    flush_interval: float = 0.2
    min_chars: int = 1


class NapcatStreamConfig(ChannelStreamConfig):
    enabled: bool = False


class ChannelConfig(BaseModel):
    type: str
    agent_id: str = "inkling"
    mention_only: bool = True
    enabled: bool = True
    stream: ChannelStreamConfig = Field(default_factory=ChannelStreamConfig)


class SlackChannelConfig(ChannelConfig):
    type: Literal["slack"] = "slack"
    app_id: str
    bot_token: SecretStr
    app_token: SecretStr
    stream: SlackStreamConfig = Field(default_factory=SlackStreamConfig)


class LarkChannelConfig(ChannelConfig):
    type: Literal["lark"] = "lark"
    app_id: str
    app_secret: SecretStr
    stream: LarkStreamConfig = Field(default_factory=LarkStreamConfig)


class NapcatChannelConfig(ChannelConfig):
    type: Literal["napcat"] = "napcat"
    stream: NapcatStreamConfig = Field(default_factory=NapcatStreamConfig)
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
        nested_model_default_partial_update=True,
        extra="ignore",
    )

    agents: AgentsConfig = Field(default_factory=AgentsConfig)
    logging: LoggingConfig = Field(default_factory=LoggingConfig)
    logfire: LogfireConfig = Field(default_factory=LogfireConfig)
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
