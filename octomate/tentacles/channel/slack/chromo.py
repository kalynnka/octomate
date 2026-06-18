from __future__ import annotations

import json
import logging
import re
import time

from octomate.schemas.conversation import ChannelAddress
from octomate.schemas.events import MessageEvent
from octomate.schemas.segments import (
    AtData,
    AtSegment,
    ImageData,
    ImageSegment,
    MessageSegment,
    ReplySegment,
    TextSegment,
)
from octomate.tentacles.channel.base import Chromo
from octomate.tentacles.channel.slack.schema import (
    SlackMessageEvent,
    SlackOutboundMessage,
    SlackThreadContext,
)

logger = logging.getLogger(__name__)

AT_RE = re.compile(r"<@(U[A-Z0-9]+)>")


class SlackChromo(Chromo[SlackMessageEvent, SlackOutboundMessage]):
    async def sip(self, raw: SlackMessageEvent) -> MessageEvent | None:
        try:
            channel_type = raw.get("channel_type", "")
            chat_type = "private" if channel_type == "im" else "group"

            message_id = raw.get("ts", "")
            thread_ts = raw.get("thread_ts", "")
            thread_id = thread_ts if thread_ts and thread_ts != message_id else ""

            text: str = raw.get("text", "")
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

            if files := raw.get("files"):
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

    def outbound_markdown(self, text: str) -> list[SlackOutboundMessage]:
        return [SlackOutboundMessage(text=text, markdown_text=text)] if text else []

    async def outbound_segments(
        self, segments: list[MessageSegment]
    ) -> list[SlackOutboundMessage]:
        # `<@U…>` is the only token Slack renders as a real mention (inverse of
        # `AT_RE`); everything else flattens to its markdown text form.
        return self.outbound_markdown(
            "\n\n".join(
                f"<@{seg.data.user_id}>" if isinstance(seg, AtSegment) else str(seg)
                for seg in segments
            )
        )

    def thread_context(self, address: ChannelAddress) -> SlackThreadContext:
        return SlackThreadContext(
            thread_ts=address.thread_id,
            recipient_user_id=address.user_id or None,
        )
