from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field
from pydantic_ai.messages import UserContent

from octomate.schemas.segments import AtSegment, MessageSegment, TextSegment
from octomate.schemas.user import UserProfile


class MessageEvent(BaseModel):
    model_config = ConfigDict(extra="ignore", coerce_numbers_to_str=True)

    tentacle_id: str = ""
    message_id: str = ""
    thread_id: str = ""
    reply_id: str = ""
    timestamp: float = 0.0
    user_id: str = ""
    chat_id: str = ""
    chat_type: Literal["private", "group"] = "private"
    self_id: str = ""
    sender: UserProfile = Field(default_factory=UserProfile)
    segments: list[MessageSegment] = Field(default_factory=list)
    raw: str = ""

    @property
    def display_name(self) -> str:
        return self.sender.name or self.sender.nickname or "anonymous"

    def is_at(self, user_id: str | None = None) -> bool:
        return user_id is not None and any(
            isinstance(seg, AtSegment) and seg.data.user_id == user_id
            for seg in self.segments
        )

    def text_parts(self) -> list[str]:
        return [
            seg.data["text"] for seg in self.segments if isinstance(seg, TextSegment)
        ]

    def __str__(self) -> str:
        body = "\n".join(f"  {seg}" for seg in self.segments)
        return f"{self.display_name} ({self.user_id}) #msg:{self.message_id}:\n{body}"

    def to_content_parts(self) -> list[UserContent]:
        header = f"{self.display_name} ({self.user_id}) #msg:{self.message_id}: "
        return [header] + [seg.to_content() for seg in self.segments]
