from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, NamedTuple

from pydantic import BaseModel, Field
from pydantic_ai.settings import ThinkingEffort

from octomate.config.agents import AgentRouteModelName, Claim
from octomate.schemas.conversation import ChannelAddress

ResponseTargetMode = Literal["main", "sub"]
# Where a summon lands: `here` transmits ownership of the current thread in place;
# `thread` hands off into a new sub-thread of the current chat. Moving to the asking
# user's DM is its own spell (`scheme`), not a destination — it hands the work to
# whoever owns that DM rather than to an agent this one picked.
SummonDestination = Literal["here", "thread"]
# How the react loop was entered, and thus what to call the agent run it drives:
# `react` (an initial reaction to an inbound message), `summon` (a handoff to another
# agent), `teleport` (the same agent resuming in a forked sub-thread), or `resume`
# (continuing after human review). Labels each run's span and any batch it defers.
RunName = Literal["react", "summon", "teleport", "resume"]


class AgentRouteKey(NamedTuple):
    agent_id: str
    model: AgentRouteModelName


@dataclass(frozen=True)
class Destination:
    """Somewhere a turn or a message can be put, named once for every spell.

    The model names a `handle` and nothing else — never a chat id, never a user id.
    That is what keeps an agent from addressing anyone it likes, and it is why the
    resolved `address` is built here rather than accepted from the model.

    `address` is what the rest of the system already speaks: `thread_manager.ensure`,
    `conversations.ensure`, `feelers.*.present` and `open_dm` all take one. A
    destination that has to be *made* before it exists carries `open_sub_thread`
    instead of a second type — its address is the parent chat.
    """

    handle: str
    # What this place is, in words, for `scry` to show.
    label: str
    address: ChannelAddress
    # The place does not exist yet: open a sub-thread of `address` and land there.
    open_sub_thread: bool = False

    def __str__(self) -> str:
        return f"- {self.handle}: {self.label}"


class SummonDecision(BaseModel):
    """A handoff decision: continue this turn with another agent, from a brief."""

    action: Literal["summon"] = "summon"
    reason: str
    agent_id: str
    model: AgentRouteModelName
    destination: SummonDestination = "thread"
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
    hint: str
    brief: str
    destination: ChannelAddress = Field(
        description="Which direct messages, resolved by the gate. The model names a "
        "handle; the address comes from the identity registry, never from the model."
    )


@dataclass(frozen=True)
class Scrying:
    """What `scry` reveals: who can take this on, and where else the asker is.

    One tool result rather than two tools, because both answer the same question —
    where can this conversation go — and a tool result is the only place a per-user
    list can reach the model without forking a cached prompt segment.
    """

    routes: list[AgentRoute]
    # Every place a spell can name, this conversation included — not only the remote
    # ones. `GatewayCapability.linked_destinations` is the remote half; this is all.
    destinations: list[Destination]

    def __str__(self) -> str:
        routes = "\n".join(str(route) for route in self.routes) or "- (none)"
        places = "\n".join(str(one) for one in self.destinations) or "- (none)"
        return (
            f"Agents you can route to:\n{routes}\n\nWhere you can put this:\n{places}"
        )


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
