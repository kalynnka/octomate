from __future__ import annotations

from typing import Annotated, Literal, TypeAlias

from pydantic import BaseModel, Field, SecretStr

from octomate.config.agents import AgentRouteModelName


class AgentModelConfig(BaseModel):
    agent: str
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


class ChatRecapConfig(BaseModel):
    """What a kick in a dm or a group chat is shown of the chat it woke in.

    Those surfaces have no end, so a kick answers in a thread of its own and its
    model context starts empty every time. Without this the agent answers a chat it
    cannot see; with all of it, the context grows with the chat room's tenure, which
    is the thing the sub-thread exists to stop.
    """

    messages: int = Field(
        default=16,
        ge=0,
        description=(
            "How many of the chat's recent messages go in front of the prompt. "
            "0 shows none, which leaves a kick with only what woke it."
        ),
    )
    characters: int = Field(
        default=1000,
        ge=0,
        description=(
            "Where each of those messages is cut. They are context, not the thing "
            "being answered, and one pasted log would otherwise be the whole slice. "
            "0 leaves them whole."
        ),
    )


class ChannelConfig(BaseModel):
    type: str
    mention_only: bool = True
    enabled: bool = True
    stream: ChannelStreamConfig = Field(default_factory=ChannelStreamConfig)
    recap: ChatRecapConfig = Field(default_factory=ChatRecapConfig)
    agents: list[AgentModelConfig] = Field(
        min_length=1,
        description=(
            "The agents this channel can dispatch to: agents[0] is the default "
            "entry agent; all of them are summon candidates. Required — nothing "
            "picks a model on an operator's behalf, so a channel with no route "
            "would have nothing to answer with."
        ),
    )
    mcp: bool = Field(
        default=False,
        description=(
            "Whether this channel offers its own MCP tools to the agents driven on "
            "it. The server is one per channel type, so the flag decides what a "
            "call may do here, never which tools an agent sees."
        ),
    )


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


class TrunklineStreamConfig(ChannelStreamConfig):
    # The console renders tokens as it receives them; stream every event
    # straight through (the timeline feeler forwards raw events, so batching
    # is moot).
    enabled: bool = True
    flush_interval: float = 0.0


class TrunklineChannelConfig(ChannelConfig):
    """The console has no group surface, so the base `mention_only` never
    applies to it (there is nothing to be mentioned in)."""

    type: Literal["trunkline"] = "trunkline"
    # Annotated as the subclass so a YAML `stream:` override keeps the
    # console defaults (the base class would flip `enabled` back to False).
    stream: TrunklineStreamConfig = Field(default_factory=TrunklineStreamConfig)


class NapcatChannelConfig(ChannelConfig):
    type: Literal["napcat"] = "napcat"
    stream: NapcatStreamConfig = Field(default_factory=NapcatStreamConfig)
    ws_url: str
    http_url: str
    access_token: SecretStr | None = None
    backoff_base: float = 1.0
    backoff_max: float = 60.0
    backoff_factor: float = 2.0


# One variant per platform, selected by `type`. Keyed by instance id rather than by
# platform, so a deployment can run two Lark apps — or two consoles — by naming them
# apart; the key is the channel tentacle id everywhere downstream, which is what
# `users[].profiles` and `Thread.channel_tentacle_id` already mean by it.
ChannelConfigVariant: TypeAlias = Annotated[
    SlackChannelConfig
    | LarkChannelConfig
    | NapcatChannelConfig
    | TrunklineChannelConfig,
    Field(discriminator="type"),
]
