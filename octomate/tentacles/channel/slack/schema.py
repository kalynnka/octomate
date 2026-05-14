from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal, TypedDict

from pydantic import Field

from octomate.schemas.conversation import UserProfile


class SlackFileInfo(TypedDict, total=False):
    mimetype: str
    url_private: str
    name: str


class SlackMessageEvent(TypedDict, total=False):
    type: Literal["message"]
    subtype: str
    bot_id: str
    user: str
    channel: str
    channel_type: str
    ts: str
    thread_ts: str
    text: str
    files: list[SlackFileInfo]


@dataclass(frozen=True)
class SlackOutboundMessage:
    text: str
    blocks: list[dict[str, Any]] | None = None


class SlackUserProfile(UserProfile):
    user_id: str = ""
    name: str = ""
    nickname: str | None = Field(default=None)
    title: str | None = None
