from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, SecretStr


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
