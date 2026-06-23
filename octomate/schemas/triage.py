from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated, Literal, TypeAlias

from pydantic import BaseModel, Field, TypeAdapter

TriageAction = Literal["direct_answer", "summon"]
ResponseTargetMode = Literal["main", "sub"]


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
    model: str
    hint: str
    summon: str

    @property
    def key(self) -> tuple[str, str]:
        return (self.agent_id, self.model)


TriageDecision: TypeAlias = Annotated[
    DirectAnswerDecision | SummonDecision,
    Field(discriminator="action"),
]
TriageDecisionAdapter = TypeAdapter(TriageDecision)


@dataclass(frozen=True)
class SummonRoute:
    agent_id: str
    model: str
    description: str

    @property
    def key(self) -> tuple[str, str]:
        return (self.agent_id, self.model)

    def __str__(self) -> str:
        model = self.model or "default"
        return f"- agent_id={self.agent_id}, model={model}: {self.description}"
