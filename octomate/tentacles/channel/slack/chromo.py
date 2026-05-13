from __future__ import annotations

import json
import logging
import re
import time
from typing import Any

from octomate.schemas.events import MessageEvent
from octomate.schemas.segments import (
    AtData,
    AtSegment,
    ImageData,
    ImageSegment,
    MarkdownSegment,
    MessageSegment,
    ReplySegment,
    TextSegment,
)
from octomate.tentacles.channel.base import PlatformMessage
from octomate.tentacles.channel.slack.schema import SlackFileInfo, SlackMessageEvent

logger = logging.getLogger(__name__)

AT_RE = re.compile(r"<@(U[A-Z0-9]+)>")
TABLE_RE = re.compile(r"((?:^\|[^\n]+\n?)+)", re.MULTILINE)


def _tables_to_code(text: str) -> str:
    def _wrap(match: re.Match) -> str:
        table = match.group(1).rstrip("\n")
        return f"```\n{table}\n```\n"

    return TABLE_RE.sub(_wrap, text)


def _md_to_mrkdwn(text: str) -> str:
    text = re.sub(r"\*{2}(.+?)\*{2}", r"*\1*", text)
    text = re.sub(r"_{2}(.+?)_{2}", r"*\1*", text)
    text = re.sub(r"~~(.+?)~~", r"~\1~", text)
    text = re.sub(r"\[(.+?)\]\((.+?)\)", r"<\2|\1>", text)
    text = re.sub(r"^#{1,6}\s+", "", text, flags=re.MULTILINE)
    return _tables_to_code(text)


class SlackChromo:
    async def sip(self, raw: SlackMessageEvent) -> MessageEvent | None:
        try:
            channel_type = raw.get("channel_type", "")
            chat_type = "private" if channel_type == "im" else "group"

            message_id = raw.get("ts", "")
            thread_ts = raw.get("thread_ts", "")
            thread_id = thread_ts if thread_ts and thread_ts != message_id else ""

            text: str = raw.get("text", "")
            segments = self._parse_segments(text, raw.get("files"))

            reply_id = ""
            if thread_ts and thread_ts != message_id:
                reply_id = thread_ts
                segments.insert(0, ReplySegment(data={"id": thread_ts}))

            return MessageEvent(
                message_id=message_id,
                thread_id=thread_id,
                reply_id=reply_id,
                timestamp=float(message_id) if message_id else time.time(),
                user_id=raw.get("user", ""),
                chat_id=raw.get("channel", ""),
                chat_type=chat_type,
                segments=segments,
                raw=json.dumps(raw, ensure_ascii=False),
            )
        except Exception:
            logger.warning("SlackChromo: failed to decode event", exc_info=True)
            return None

    async def squirt(
        self,
        segments: list[MessageSegment],
        *,
        reply_to: str | None = None,
    ) -> list[PlatformMessage]:
        if not segments:
            return []

        result: list[PlatformMessage] = []
        blocks: list[dict[str, Any]] = []
        content_parts: list[str] = []

        for seg in segments:
            if isinstance(seg, (MarkdownSegment, TextSegment, AtSegment)):
                if isinstance(seg, MarkdownSegment):
                    text = _md_to_mrkdwn(seg.data["text"])
                elif isinstance(seg, AtSegment):
                    text = f"<@{seg.data.user_id}>"
                else:
                    text = seg.data["text"]
                blocks.append(
                    {"type": "section", "text": {"type": "mrkdwn", "text": text}}
                )
                content_parts.append(text)
            elif isinstance(seg, ImageSegment) and seg.data.url:
                blocks.append(
                    {
                        "type": "image",
                        "image_url": seg.data.url,
                        "alt_text": seg.data.summary or seg.data.name or "image",
                    }
                )
                content_parts.append("[image]")

        if blocks:
            result.append(
                PlatformMessage(
                    msg_type="blocks",
                    content=" ".join(content_parts),
                    metadata={"blocks": blocks},
                )
            )
        return result

    def _parse_segments(
        self,
        text: str,
        files: list[SlackFileInfo] | None,
    ) -> list[MessageSegment]:
        segments: list[MessageSegment] = []

        if text:
            cursor = 0
            for match in AT_RE.finditer(text):
                if match.start() > cursor:
                    segments.append(
                        TextSegment(data={"text": text[cursor : match.start()]})
                    )
                segments.append(AtSegment(data=AtData(user_id=match.group(1))))
                cursor = match.end()
            if cursor < len(text):
                segments.append(TextSegment(data={"text": text[cursor:]}))

        if files:
            for file_info in files:
                mimetype = file_info.get("mimetype", "")
                if not mimetype.startswith("image/"):
                    continue
                url = file_info.get("url_private", "")
                segments.append(
                    ImageSegment(
                        data=ImageData(
                            file=url,
                            url=url,
                            name=file_info.get("name", ""),
                        )
                    )
                )

        return segments
