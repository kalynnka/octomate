from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated, Literal, NamedTuple, TypeAlias

from pydantic import BaseModel, ConfigDict, Field
from pydantic_ai.settings import ThinkingEffort

from octomate.config.agents import AgentRouteModelName, Claim
from octomate.schemas.conversation import ChannelAddress

ResponseTargetMode = Literal["main", "sub"]
# How the react loop was entered, and thus what to call the agent run it drives:
# `react` (an initial reaction to an inbound message), `summon` (a handoff to another
# agent), `teleport` (the same agent resuming in a forked sub-thread), or `resume`
# (continuing after human review). Labels each run's span and any batch it defers.
RunName = Literal["react", "summon", "teleport", "resume"]

# The gateway's vocabulary: the toolset id and each spell's tool name. They live with
# the decision schemas rather than the capability because everyone speaks them — the
# policy layer's refusal sentences, the reflex graph, channel rendering — and none of
# those should have to import an agent capability for a string. `send` is the one
# exception: its name is `octomate.schemas.messages.SEND_TOOL_NAME`, beside the
# segment types it delivers.
GATEWAY_TOOLSET_ID = "gateway"
SCRY_TOOL_NAME = "scry"
SUMMON_TOOL_NAME = "summon"
TELEPORT_TOOL_NAME = "teleport"
SCHEME_TOOL_NAME = "scheme"
COMMISSION_TOOL_NAME = "commission"
WHISPER_TOOL_NAME = "whisper"
# What one `scry` reveals. One facet per call, because each spell needs exactly one —
# a route for `summon`, a place for anything that lands somewhere — and the routes
# alone run long enough that showing everything every time buried the line the caller
# came for. A tool result is the only place a per-user list can reach the model
# without forking a cached prompt segment.
ScryFacet = Literal["routes", "destinations"]
# The `teleport` deferral's declared metadata kind. The suspender and dispatch graph
# classify the deferral by this kind rather than the tool name, so the gateway (which
# emits it) and `reflex` (which resolves it) agree on one value without matching on
# the name.
TELEPORT_DEFER_KIND = "teleport"


class AgentRouteKey(NamedTuple):
    agent_id: str
    model: AgentRouteModelName


class SpellTarget(BaseModel):
    """Base of the places a spell's `destination` argument can name.

    A variant per place rather than one free string, so a tool definition says what
    it accepts instead of documenting it in prose. Only the channel one carries a
    value, and it has to: which channels a person is on is runtime state, and a tool
    definition that varied with it would fork the provider's prompt cache at the very
    front of the prefix. So the *shape* is declared here and the *policy* — which of
    these this surface can actually reach — stays a refusal in the tool body.

    `handle` is what the gateway resolves against, and what `scry` prints.
    """

    model_config = ConfigDict(frozen=True)

    @property
    def handle(self) -> str:
        raise NotImplementedError


class HereTarget(SpellTarget):
    """This conversation, as it stands."""

    kind: Literal["here"] = "here"

    @property
    def handle(self) -> str:
        return "here"


class ThreadTarget(SpellTarget):
    """A new sub-thread of this chat."""

    kind: Literal["thread"] = "thread"

    @property
    def handle(self) -> str:
        return "thread"


class DirectTarget(SpellTarget):
    """The asking user's direct messages on this channel."""

    kind: Literal["dm"] = "dm"

    @property
    def handle(self) -> str:
        return "dm"


class ChannelTarget(SpellTarget):
    """Another channel this person is on, to reach them where they already are."""

    kind: Literal["channel"] = "channel"
    channel: str = Field(
        description="The channel id, copied exactly from a `scry` destination."
    )

    @property
    def handle(self) -> str:
        return self.channel


# One union per spell, naming exactly the places that spell can go. They differ:
# `here` is where a summon hands over and a send delivers, but a teleport that
# stayed put would just be the agent carrying on; `dm` is where a scheme lands,
# and a summon into someone's direct messages is a scheme by another name.
SummonTarget: TypeAlias = Annotated[
    HereTarget | ThreadTarget | ChannelTarget, Field(discriminator="kind")
]
TeleportTarget: TypeAlias = Annotated[
    ThreadTarget | ChannelTarget, Field(discriminator="kind")
]
SchemeTarget: TypeAlias = Annotated[
    DirectTarget | ChannelTarget, Field(discriminator="kind")
]
SendTarget: TypeAlias = Annotated[
    HereTarget | DirectTarget | ChannelTarget, Field(discriminator="kind")
]

# The three that carry nothing are the same value every time, so they are made once:
# a spell defaults to one, and `built_in_destinations` takes its handles from them
# rather than repeating the strings the variants already own.
HERE_TARGET = HereTarget()
THREAD_TARGET = ThreadTarget()
DIRECT_TARGET = DirectTarget()


@dataclass(frozen=True)
class Destination:
    """Somewhere a turn or a message can be put, named once for every spell.

    The model names a `handle` and nothing else — never a chat id, never a user id.
    That is what keeps an agent from addressing anyone it likes, and it is why the
    resolved `address` is built here rather than accepted from the model.

    `address` is what the rest of the system already speaks: `thread_manager.ensure`,
    `conversations.ensure`, `feelers.*.present` and `open_dm` all take one. Every
    destination names somewhere that already exists and someone can be reached at,
    which is why a sub-thread — a place made on the way — is not one of them.
    """

    handle: str
    # What this place is, in words, for `scry` to show.
    label: str
    address: ChannelAddress
    # Who can take a handoff there, when that is not the same list as here: which
    # agents serve a channel is that channel's own config, so a place on another one
    # answers with its own. Empty for this run's own surface, whose routes `scry`
    # already lists whole, and for a place only `send` and `scheme` can reach.
    routes: tuple[AgentRoute, ...] = ()

    def __str__(self) -> str:
        line = f"- {self.handle}: {self.label}"
        if not self.routes:
            return line
        return "\n".join([line, *(f"  {route}" for route in self.routes)])


class HereLanding(BaseModel):
    """Take over this same conversation, in place — no new surface."""

    kind: Literal["here"] = "here"


class ThreadLanding(BaseModel):
    """Open a new sub-thread of the current chat and land inside it. Carries no
    address: the node already holds the one the run is on."""

    kind: Literal["thread"] = "thread"


class CrossingLanding(BaseModel):
    """Open a sub-thread of this person's direct messages on another channel.

    A landing of its own rather than a `ThreadLanding` with an address, because
    reaching it takes a different sequence: the direct messages have to be opened
    before there is anywhere to open a sub-thread of, the receiving agent is
    resolved against the far channel's config, and the origin has to be told,
    having watched the conversation leave without a word.
    """

    kind: Literal["crossing"] = "crossing"
    address: ChannelAddress = Field(
        description="The channel and the account on it, from the identity registry. "
        "`chat_id` stays empty until that channel opens the conversation."
    )


SummonLanding: TypeAlias = Annotated[
    HereLanding | ThreadLanding | CrossingLanding, Field(discriminator="kind")
]


class SummonDecision(BaseModel):
    """A handoff decision: continue this turn with another agent, from a brief."""

    action: Literal["summon"] = "summon"
    reason: str
    agent_id: str
    model: AgentRouteModelName
    destination: SummonLanding = Field(
        default_factory=ThreadLanding,
        description="Where the handoff lands, resolved by the gateway. The model names "
        "a handle; an address is built here, never accepted from it.",
    )
    effort: ThinkingEffort | None = None
    hint: str
    summon: str

    @property
    def key(self) -> AgentRouteKey:
        return AgentRouteKey(agent_id=self.agent_id, model=self.model)


class SchemeDecision(BaseModel):
    """Take this turn to the asking user's DM, from a brief.

    No agent is named: whoever already handles that user's DM picks the work up, so a
    group can never point someone's private assistant at an agent it chose. The
    receiver is resolved against the DM's own thread, which is why this carries a brief
    rather than a route.
    """

    action: Literal["scheme"] = "scheme"
    hint: str = Field(
        description="The line that opens the conversation over there. Nothing is "
        "posted where the request came from — the run's own reply closes that out."
    )
    brief: str
    destination: ChannelAddress = Field(
        description="Which direct messages, resolved by the gateway. The model names a "
        "handle; the address comes from the identity registry, never from the model."
    )


class TeleportDecision(BaseModel):
    """The same agent continues in a new sub-thread; Reflex performs the move.

    Inkling never records one — its teleport rides a deferral so the graph can fork
    mid-run — but a runtime that cannot be suspended by a tool result reports the
    same intent this way, and the graph forks after its turn ends instead.
    """

    action: Literal["teleport"] = "teleport"
    hint: str = Field(description="The short, user-facing thread-starter message.")
    crossing: CrossingLanding | None = Field(
        default=None,
        description="The far channel's direct messages when the teleport crosses, "
        "resolved by the gateway; None keeps it a sub-thread of the current chat.",
    )


# Every decision a gateway can record for the graph to act on after the turn.
GatewayDecision: TypeAlias = Annotated[
    SummonDecision | SchemeDecision | TeleportDecision, Field(discriminator="action")
]


@dataclass(frozen=True)
class AgentRoute:
    """A summonable (agent, model) pair and the claim it advertises. Agents
    advertise; the caller requests — the claim publishes the space this route
    supports, and a caller picks a point in it."""

    agent_id: str
    model: AgentRouteModelName
    claim: Claim

    @property
    def key(self) -> AgentRouteKey:
        return AgentRouteKey(agent_id=self.agent_id, model=self.model)

    def __str__(self) -> str:
        return f"- agent_id={self.agent_id}, model={self.model!r}: {self.claim}"
