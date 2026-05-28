from __future__ import annotations

from typing import Literal

from pydantic import BaseModel

TriageAction = Literal["answer", "reception"]
ResponseTargetMode = Literal["main", "sub"]


class TriageDecision(BaseModel):
    action: TriageAction
    answer: str = ""
    target_id: str = ""
    reason: str = ""
    hint: str = ""
    handoff: str = ""
