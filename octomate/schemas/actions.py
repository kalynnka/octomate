from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

from octomate.schemas.segments import AgentSegment


class AgentMessage(BaseModel):
    """A single outgoing message composed of one or more segments."""

    segments: list[AgentSegment]

    def __str__(self) -> str:
        return "".join(str(seg) for seg in self.segments)


class ActionResponse(BaseModel):
    """Response received after sending an action via WebSocket."""

    status: str = ""
    retcode: int = 0
    data: dict[str, Any] | None = None
    echo: str | None = None
    message: str | None = None
    wording: str | None = None


class SendGroupMsgParams(BaseModel):
    group_id: int | str
    message: list[AgentSegment]
    reply: int | str | None = None


class SendGroupMsgAction(BaseModel):
    action: Literal["send_group_msg"] = "send_group_msg"
    tentacle_id: str
    params: SendGroupMsgParams


class SendPrivateMsgParams(BaseModel):
    user_id: int | str
    message: list[AgentSegment]
    reply: int | str | None = None


class SendPrivateMsgAction(BaseModel):
    action: Literal["send_private_msg"] = "send_private_msg"
    tentacle_id: str
    params: SendPrivateMsgParams


class CallApiAction(BaseModel):
    action: str
    tentacle_id: str
    params: dict[str, Any] = Field(default_factory=dict)
