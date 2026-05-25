from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal, NotRequired, TypedDict

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
    team: str
    team_id: str
    enterprise_id: str
    channel: str
    channel_type: str
    ts: str
    thread_ts: str
    text: str
    files: list[SlackFileInfo]


class SlackPostMessageKwargs(TypedDict):
    channel: str
    text: str
    blocks: NotRequired[list[dict[str, Any]]]
    thread_ts: NotRequired[str]


@dataclass(frozen=True)
class SlackOutboundMessage:
    text: str
    markdown_text: str | None = None
    blocks: list[dict[str, Any]] | None = None


@dataclass(frozen=True)
class SlackThreadContext:
    thread_ts: str
    recipient_user_id: str | None = None
    recipient_team_id: str | None = None


class SlackUserProfile(UserProfile):
    user_id: str = ""
    name: str = ""
    nickname: str | None = Field(default=None)
    title: str | None = None
