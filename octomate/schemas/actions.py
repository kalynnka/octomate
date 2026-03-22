from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

from octomate.schemas.segments import AgentSegment
from octomate.schemas.session import SessionKey


class AgentMessage(BaseModel):
    """A single outgoing message composed of one or more segments."""

    segments: list[AgentSegment]

    def __str__(self) -> str:
        return "".join(str(seg) for seg in self.segments)


class ConfirmAction(BaseModel):
    confirmation_id: str
    session_key: SessionKey
    tool_name: str
    tool_call_id: str
    args: dict[str, Any]
    title: str = ""
    description: str = ""
    skill: str = ""
    approvers: list[str] = Field(default_factory=list)
    created_at: float
    expires_at: float
    status: Literal["pending", "approved", "denied", "expired"] = "pending"
