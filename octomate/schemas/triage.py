from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated, Literal, NamedTuple, TypeAlias

from pydantic import BaseModel, Field, TypeAdapter

from octomate.config.agents import AgentRouteModelName

TriageAction = Literal["direct_answer", "summon"]
ResponseTargetMode = Literal["main", "sub"]


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
