from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated, Literal, NamedTuple, TypeAlias

from pydantic import BaseModel, Field, TypeAdapter

from octomate.config.agents import AgentRouteModelName

TriageAction = Literal["direct_answer", "summon"]
ResponseTargetMode = Literal["main", "sub"]
# Where a summon lands: `here` transmits ownership of the current thread in place;
# `thread` hands off into a new sub-thread of the current chat. (A brand-new DM and
# cross-channel targets are parked — see docs/plans/self-routing-dispatch.md.)
SummonDestination = Literal["here", "thread"]


class SummonRouteKey(NamedTuple):
    agent_id: str
    model: AgentRouteModelName


class TriageDecisionBase(BaseModel):
    action: TriageAction
    reason: str


class DirectAnswerDecision(TriageDecisionBase):
    action: Literal["direct_answer"]
    target_id: str
    answer: str


class SummonDecision(TriageDecisionBase):
    action: Literal["summon"]
    agent_id: str
    model: AgentRouteModelName
    destination: SummonDestination = "thread"
    hint: str
    summon: str

    @property
    def key(self) -> SummonRouteKey:
        return SummonRouteKey(agent_id=self.agent_id, model=self.model)


TriageDecision: TypeAlias = Annotated[
    DirectAnswerDecision | SummonDecision,
    Field(discriminator="action"),
]
TriageDecisionAdapter = TypeAdapter(TriageDecision)


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
