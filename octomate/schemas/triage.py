from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, NamedTuple

from pydantic import BaseModel

from octomate.config.agents import AgentRouteModelName

ResponseTargetMode = Literal["main", "sub"]
# Where a summon lands: `here` transmits ownership of the current thread in place;
# `thread` hands off into a new sub-thread of the current chat. (A brand-new DM and
# cross-channel targets are parked — see docs/plans/self-routing-dispatch.md.)
SummonDestination = Literal["here", "thread"]
# How the react loop was entered, and thus what to call the agent run it drives:
# `react` (an initial reaction to an inbound message), `summon` (a handoff to another
# agent), `teleport` (the same agent resuming in a forked sub-thread), or `resume`
# (continuing after human review). Labels each run's span and any batch it defers.
RunName = Literal["react", "summon", "teleport", "resume"]


class SummonRouteKey(NamedTuple):
    agent_id: str
    model: AgentRouteModelName


class SummonDecision(BaseModel):
    """A handoff decision: continue this turn with another agent, from a brief."""

    action: Literal["summon"] = "summon"
    reason: str
    agent_id: str
    model: AgentRouteModelName
    destination: SummonDestination = "thread"
    hint: str
    summon: str

    @property
    def key(self) -> SummonRouteKey:
        return SummonRouteKey(agent_id=self.agent_id, model=self.model)


@dataclass(frozen=True)
class SummonRoute:
    agent_id: str
    model: AgentRouteModelName
    description: str

    @property
    def key(self) -> SummonRouteKey:
        return SummonRouteKey(agent_id=self.agent_id, model=self.model)

    def __str__(self) -> str:
        return f"- agent_id={self.agent_id}, model={self.model!r}: {self.description}"
