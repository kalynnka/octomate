from __future__ import annotations

from typing import Annotated, Literal, TypeAlias

from pydantic import BaseModel, Field, TypeAdapter

TriageAction = Literal["direct_answer", "handoff"]
ResponseTargetMode = Literal["main", "sub"]


class TriageDecisionBase(BaseModel):
    action: TriageAction
    reason: str


class DirectAnswerDecision(TriageDecisionBase):
    action: Literal["direct_answer"]
    target_id: str
    answer: str


class HandoffDecision(TriageDecisionBase):
    action: Literal["handoff"]
    target_id: str
    agent_id: str
    model: str
    hint: str
    handoff: str


TriageDecision: TypeAlias = Annotated[
    DirectAnswerDecision | HandoffDecision,
    Field(discriminator="action"),
]
TriageDecisionAdapter = TypeAdapter(TriageDecision)
