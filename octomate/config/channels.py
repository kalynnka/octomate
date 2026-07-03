from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, SecretStr

from octomate.config.agents import AgentRouteModelName

DEFAULT_TRIAGE_MODEL: AgentRouteModelName = "deepseek:deepseek-v4-flash"
DEFAULT_RECEPTION_MODEL: AgentRouteModelName = "deepseek:deepseek-v4-pro"


class AgentModelConfig(BaseModel):
    agent: str = "inkling"
    model: AgentRouteModelName


class ChannelStreamConfig(BaseModel):
    enabled: bool = False
    flush_interval: float = 0.5
    min_chars: int = 20
    max_chars: int = 1000
    fold_threshold: int = 1500


class SlackStreamConfig(ChannelStreamConfig):
    # Slack streams via `chat.appendStream`, one API call per flush. `flush_interval`
    # alone paces the edits (~2/s) — keep `min_chars` low so short answers stream on
    # that cadence instead of being held until they reach 120 chars.
    enabled: bool = True
    flush_interval: float = 0.2
    min_chars: int = 20


class LarkStreamConfig(ChannelStreamConfig):
    enabled: bool = True
    flush_interval: float = 0.2
    min_chars: int = 20


class NapcatStreamConfig(ChannelStreamConfig):
    enabled: bool = False


class ChannelConfig(BaseModel):
    type: str
    mention_only: bool = True
    enabled: bool = True
    stream: ChannelStreamConfig = Field(default_factory=ChannelStreamConfig)
    triage: AgentModelConfig = AgentModelConfig(model=DEFAULT_TRIAGE_MODEL)
    receptions: list[AgentModelConfig] = [
        AgentModelConfig(model=DEFAULT_RECEPTION_MODEL)
    ]


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


class VercelStreamConfig(ChannelStreamConfig):
    # The dev UI renders tokens as they arrive; stream every event straight
    # through (the timeline feeler forwards raw events, so batching is moot).
    enabled: bool = True
    flush_interval: float = 0.0


class VercelChannelConfig(ChannelConfig):
    type: Literal["vercel"] = "vercel"
    mention_only: Literal[False] = False  # The dev UI is always mention-free.
    stream: ChannelStreamConfig = Field(default_factory=VercelStreamConfig)


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
    # The dev UI ships on by default; disable it with `enabled: false` or `null`.
    # Keyed `dev_ui` to match the channel id it is registered under.
    dev_ui: VercelChannelConfig | None = Field(default_factory=VercelChannelConfig)
