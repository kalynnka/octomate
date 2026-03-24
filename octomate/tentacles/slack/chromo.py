from __future__ import annotations

import json
import logging
import re
import time
from typing import Any

from octomate.schemas.events import MessageEvent
from octomate.schemas.segments import (
    AgentSegment,
    AtData,
    AtSegment,
    ImageData,
    ImageSegment,
    MarkdownSegment,
    ReplySegment,
    TextSegment,
)
from octomate.tentacles.base import PlatformMessage

logger = logging.getLogger(__name__)

_AT_RE = re.compile(r"<@(U[A-Z0-9]+)>")


def _md_to_mrkdwn(text: str) -> str:
    # Code fences: keep as-is (Slack supports ```)
    # Bold: **text** or __text__ → *text*
    text = re.sub(r"\*{2}(.+?)\*{2}", r"*\1*", text)
    text = re.sub(r"_{2}(.+?)_{2}", r"*\1*", text)
    # Italic: _text_ stays the same in Slack mrkdwn
    # Strikethrough: ~~text~~ → ~text~
    text = re.sub(r"~~(.+?)~~", r"~\1~", text)
    # Links: [text](url) → <url|text>
    text = re.sub(r"\[(.+?)\]\((.+?)\)", r"<\2|\1>", text)
    # Headers: strip # prefix (Slack has no header markdown)
    text = re.sub(r"^#{1,6}\s+", "", text, flags=re.MULTILINE)
    # Blockquotes: > stays the same in Slack mrkdwn
    return text


class SlackChromo:
    async def sip(self, raw: dict[str, Any]) -> MessageEvent | None:
        try:
            channel_type = raw.get("channel_type", "")
            chat_type = "private" if channel_type == "im" else "group"

            ts = raw.get("ts", "")
            thread_ts = raw.get("thread_ts", "")
            thread_id = thread_ts if thread_ts and thread_ts != ts else ""

            text: str = raw.get("text", "")
            segments = self._parse_segments(text, raw.get("files"))

            reply_id = ""
            if thread_ts and thread_ts != ts:
                reply_id = thread_ts
                segments.insert(0, ReplySegment(data={"id": thread_ts}))

            user_id = raw.get("user", "")
            channel = raw.get("channel", "")

            return MessageEvent(
                message_id=ts,
                thread_id=thread_id,
                reply_id=reply_id,
                timestamp=float(ts) if ts else time.time(),
                user_id=user_id,
                chat_id=channel,
                chat_type=chat_type,
                segments=segments,
                raw=json.dumps(raw, ensure_ascii=False),
            )
        except Exception:
            logger.warning("SlackChromo: failed to decode event", exc_info=True)
            return None

    async def squirt(
        self, segments: list[AgentSegment], *, reply_to: str | None = None
    ) -> list[PlatformMessage]:
        result: list[PlatformMessage] = []
        for seg in segments:
            msg = self._encode_segment(seg)
            if msg is not None:
                result.append(msg)
        return result

    def _encode_segment(self, seg: AgentSegment) -> PlatformMessage | None:
        if isinstance(seg, MarkdownSegment):
            text = _md_to_mrkdwn(seg.data["text"])
            blocks = [{"type": "markdown", "text": text}]
            return PlatformMessage(
                msg_type="blocks",
                content=text,
                metadata={"blocks": blocks},
            )
        if isinstance(seg, TextSegment):
            text = seg.data["text"]
            blocks = [{"type": "markdown", "text": text}]
            return PlatformMessage(
                msg_type="blocks",
                content=text,
                metadata={"blocks": blocks},
            )
        if isinstance(seg, AtSegment):
            text = f"<@{seg.data.user_id}>"
            blocks = [{"type": "markdown", "text": text}]
            return PlatformMessage(
                msg_type="blocks",
                content=text,
                metadata={"blocks": blocks},
            )
        if isinstance(seg, ImageSegment) and seg.data.url:
            blocks = [
                {
                    "type": "image",
                    "image_url": seg.data.url,
                    "alt_text": seg.data.summary or seg.data.name or "image",
                }
            ]
            return PlatformMessage(
                msg_type="blocks",
                content="[image]",
                metadata={"blocks": blocks},
            )
        return None

    def _parse_segments(self, text: str, files: list[dict[str, Any]] | None) -> list:
        segments: list = []
        if text:
            remaining = text
            for match in _AT_RE.finditer(text):
                start, end = match.span()
                before = remaining[: start - (len(text) - len(remaining))]
                if before:
                    segments.append(TextSegment(data={"text": before}))
                segments.append(AtSegment(data=AtData(user_id=match.group(1))))
                remaining = text[end:]
            if not segments:
                segments.append(TextSegment(data={"text": text}))
            elif remaining:
                segments.append(TextSegment(data={"text": remaining}))

        if files:
            for f in files:
                mimetype = f.get("mimetype", "")
                if mimetype.startswith("image/"):
                    url = f.get("url_private", "")
                    segments.append(
                        ImageSegment(
                            data=ImageData(
                                file=url,
                                url=url,
                                name=f.get("name", ""),
                            )
                        )
                    )

        return segments
