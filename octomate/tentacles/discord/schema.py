"""Discord outbound message shape."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import discord
from pydantic import BaseModel
from typing_extensions import TypedDict


class DiscordMessageReference(TypedDict):
    message_id: str | None
    channel_id: str
    guild_id: str | None
    author_id: str | None


class DiscordMention(TypedDict):
    id: str
    name: str


class DiscordAttachment(TypedDict):
    id: str
    filename: str
    url: str
    content_type: str | None
    size: int


class DiscordMessageSnapshot(BaseModel):
    id: str
    type: str
    content: str
    author_id: str
    channel_id: str
    guild_id: str | None
    reference: DiscordMessageReference | None
    mentions: list[DiscordMention]
    attachments: list[DiscordAttachment]


@dataclass(frozen=True)
class DiscordOutboundMessage:
    """One message to send: its content, attachments, the users it may ping and
    an optional component view."""

    content: str = ""
    attachment_paths: tuple[Path, ...] = ()
    mentioned_user_ids: tuple[str, ...] = ()
    view: discord.ui.View | discord.ui.LayoutView | None = None
