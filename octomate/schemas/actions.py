from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from octomate.schemas.conversation import ChannelAddress
from octomate.schemas.segments import MessageSegment
from octomate.types.json import JsonObject


class AgentMessage(BaseModel):
    """A single outgoing message composed of one or more segments."""

    segments: list[MessageSegment]

    def __str__(self) -> str:
        return "".join(str(seg) for seg in self.segments)


class ConfirmAction(BaseModel):
    confirmation_id: str
    conversation_address: ChannelAddress
    tool_name: str
    tool_call_id: str
    args: JsonObject
    title: str = ""
    description: str = ""
    skill: str = ""
    approvers: list[str] = Field(default_factory=list)
    created_at: float
    expires_at: float
    status: Literal["pending", "approved", "denied", "expired"] = "pending"


class QuestionAction(BaseModel):
    question_id: str
    conversation_address: ChannelAddress
    text: str
    options: list[str] = Field(default_factory=list)
    multi_select: bool = False
    created_at: float
    expires_at: float
    status: Literal["pending", "answered", "expired"] = "pending"


class QuestionResponse(BaseModel):
    question_id: str
    answer: str
    responder_id: str


class TodoAction(BaseModel):
    todo_id: str
    title: str
    active_form: str = ""
    assignee: str = ""
    status: Literal["pending", "in_progress", "completed", "cancelled"] = "pending"
