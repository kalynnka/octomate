from __future__ import annotations

import json
import logging
import re
from pathlib import Path

import discord

from octomate.schemas.events import MessageEvent
from octomate.schemas.segments import (
    AtData,
    AtSegment,
    FileSegment,
    ImageData,
    ImageSegment,
    MarkdownSegment,
    MessageSegment,
    ReplyData,
    ReplySegment,
    TextSegment,
)
from octomate.tentacles.channel import Chromo
from octomate.tentacles.discord.schema import DiscordOutboundMessage
from octomate.tentacles.feelers.output import MarkdownChunker

logger = logging.getLogger(__name__)

DISCORD_MENTION_RE = re.compile(r"<@!?(\d+)>")
DISCORD_MARKDOWN_CHUNKER = MarkdownChunker(limit=2000)
SUPPORTED_MESSAGE_TYPES = (discord.MessageType.default, discord.MessageType.reply)


class DiscordChromo(Chromo[discord.Message, DiscordOutboundMessage]):
    async def sip(self, raw: discord.Message) -> MessageEvent | None:
        try:
            if raw.type not in SUPPORTED_MESSAGE_TYPES:
                return None

            channel = raw.channel
            channel_thread_id: str | None = None
            if isinstance(channel, discord.DMChannel):
                chat_type = "dm"
                chat_id = str(channel.id)
                shared = False
            elif isinstance(channel, discord.Thread):
                if channel.parent_id is None:
                    return None
                chat_type = "thread"
                chat_id = str(channel.parent_id)
                channel_thread_id = str(channel.id)
                shared = True
            elif isinstance(channel, discord.TextChannel):
                chat_type = "group"
                chat_id = str(channel.id)
                shared = True
            else:
                return None

            segments: list[MessageSegment] = []
            reply_id = ""
            reference = raw.reference
            if reference is not None and reference.message_id is not None:
                reply_id = str(reference.message_id)
                reply_data = ReplyData(id=reply_id)
                if isinstance(reference.resolved, discord.Message):
                    reply_data["user_id"] = str(reference.resolved.author.id)
                segments.append(ReplySegment(data=reply_data))

            mention_names = {str(user.id): user.display_name for user in raw.mentions}
            cursor = 0
            for match in DISCORD_MENTION_RE.finditer(raw.content):
                if match.start() > cursor:
                    segments.append(
                        TextSegment(data={"text": raw.content[cursor : match.start()]})
                    )
                user_id = match.group(1)
                segments.append(
                    AtSegment(
                        data=AtData(user_id=user_id, name=mention_names.get(user_id))
                    )
                )
                cursor = match.end()
            if cursor < len(raw.content):
                segments.append(TextSegment(data={"text": raw.content[cursor:]}))

            for attachment in raw.attachments:
                if not (attachment.content_type or "").startswith("image/"):
                    continue
                segments.append(
                    ImageSegment(
                        data=ImageData(
                            file=attachment.url,
                            url=attachment.url,
                            name=attachment.filename,
                            summary=attachment.description,
                        )
                    )
                )

            snapshot_reference = None
            if reference is not None:
                snapshot_reference = {
                    "message_id": (
                        str(reference.message_id)
                        if reference.message_id is not None
                        else None
                    ),
                    "channel_id": str(reference.channel_id),
                    "guild_id": (
                        str(reference.guild_id)
                        if reference.guild_id is not None
                        else None
                    ),
                    "author_id": (
                        str(reference.resolved.author.id)
                        if isinstance(reference.resolved, discord.Message)
                        else None
                    ),
                }
            snapshot = {
                "id": str(raw.id),
                "type": raw.type.name,
                "content": raw.content,
                "author_id": str(raw.author.id),
                "channel_id": str(channel.id),
                "guild_id": str(raw.guild.id) if raw.guild is not None else None,
                "reference": snapshot_reference,
                "mentions": [
                    {"id": str(user.id), "name": user.display_name}
                    for user in raw.mentions
                ],
                "attachments": [
                    {
                        "id": str(attachment.id),
                        "filename": attachment.filename,
                        "url": attachment.url,
                        "content_type": attachment.content_type,
                        "size": attachment.size,
                    }
                    for attachment in raw.attachments
                ],
            }
            return MessageEvent(
                message_id=str(raw.id),
                channel_thread_id=channel_thread_id,
                reply_id=reply_id,
                timestamp=raw.created_at.timestamp(),
                user_id=str(raw.author.id),
                chat_id=chat_id,
                chat_type=chat_type,
                shared=shared,
                segments=segments,
                raw=json.dumps(snapshot, ensure_ascii=False, separators=(",", ":")),
            )
        except Exception:
            logger.warning("DiscordChromo: failed to decode message", exc_info=True)
            return None

    def outbound_markdown(self, text: str) -> list[DiscordOutboundMessage]:
        return self._chunk_content(text)

    async def outbound_segments(
        self, segments: list[MessageSegment]
    ) -> list[DiscordOutboundMessage]:
        pieces: list[str] = []
        mention_offsets: list[tuple[int, str]] = []
        attachment_paths: list[Path] = []
        content_length = 0

        for segment in segments:
            match segment:
                case AtSegment():
                    rendered = f"<@{segment.data.user_id}>"
                    user_id = segment.data.user_id
                case TextSegment() | MarkdownSegment():
                    rendered = segment.data["text"]
                    user_id = None
                case ImageSegment():
                    attachment_paths.append(segment.data.path)
                    continue
                case FileSegment():
                    attachment_paths.append(Path(segment.data.file))
                    continue
                case _:
                    rendered = str(segment)
                    user_id = None

            if not rendered:
                continue
            if pieces:
                pieces.append("\n\n")
                content_length += 2
            if user_id is not None:
                mention_offsets.append((content_length, user_id))
            pieces.append(rendered)
            content_length += len(rendered)

        return self._chunk_content(
            "".join(pieces),
            mention_offsets=tuple(mention_offsets),
            attachment_paths=tuple(attachment_paths),
        )

    def _chunk_content(
        self,
        content: str,
        *,
        mention_offsets: tuple[tuple[int, str], ...] = (),
        attachment_paths: tuple[Path, ...] = (),
    ) -> list[DiscordOutboundMessage]:
        if not content:
            return (
                [DiscordOutboundMessage(attachment_paths=attachment_paths)]
                if attachment_paths
                else []
            )

        messages: list[DiscordOutboundMessage] = []
        offset = 0
        for index, chunk in enumerate(DISCORD_MARKDOWN_CHUNKER.chunk(content)):
            end = offset + len(chunk)
            mentioned_user_ids: list[str] = []
            for mention_offset, user_id in mention_offsets:
                if offset <= mention_offset < end and user_id not in mentioned_user_ids:
                    mentioned_user_ids.append(user_id)
            messages.append(
                DiscordOutboundMessage(
                    content=chunk,
                    attachment_paths=attachment_paths if index == 0 else (),
                    mentioned_user_ids=tuple(mentioned_user_ids),
                )
            )
            offset = end
        return messages
