from __future__ import annotations

import json
import logging
import time
from typing import Any, Literal

from lark_oapi.api.im.v1.model.p2_im_message_receive_v1 import P2ImMessageReceiveV1

from octomate.schemas.events import MessageEvent
from octomate.schemas.segments import (
    AgentSegment,
    AtData,
    AtSegment,
    ImageData,
    ImageSegment,
    MarkdownSegment,
    MessageSegment,
    TextSegment,
)
from octomate.tentacles.base import PlatformMessage

logger = logging.getLogger(__name__)


class LarkChromo:
    async def sip(self, raw: P2ImMessageReceiveV1) -> MessageEvent | None:
        try:
            event = raw.event
            if event is None or event.message is None or event.sender is None:
                return None

            message = event.message
            msg_type = message.message_type
            chat_type = message.chat_type
            if not msg_type or not chat_type:
                return None

            segments = self._parse_segments(msg_type, message.content, message.mentions)

            sender_id_obj = event.sender.sender_id
            sender_id: str = (sender_id_obj.open_id or "") if sender_id_obj else ""

            lark_chat_type: Literal["private", "group"]
            if chat_type == "group":
                lark_chat_type = "group"
                chat_id = message.chat_id or ""
            elif chat_type == "p2p":
                lark_chat_type = "private"
                chat_id = sender_id
            else:
                logger.warning("LarkChromo: unsupported chat_type %r", chat_type)
                return None

            return MessageEvent(
                message_id=message.message_id or "",
                timestamp=time.time(),
                user_id=sender_id,
                chat_id=chat_id,
                chat_type=lark_chat_type,
                segments=segments,
                raw=message.content or "",
            )
        except Exception:
            logger.warning("LarkChromo: failed to decode event", exc_info=True)
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
            elements = [{"tag": "markdown", "content": seg.data["text"]}]
            content = json.dumps({"schema": "2.0", "body": {"elements": elements}})
            return PlatformMessage(msg_type="interactive", content=content)
        if isinstance(seg, TextSegment):
            return PlatformMessage(
                msg_type="text", content=json.dumps({"text": seg.data["text"]})
            )
        if isinstance(seg, AtSegment):
            at_text = f'<at user_id="{seg.data.user_id}">{seg.data.name or ""}</at>'
            return PlatformMessage(
                msg_type="text", content=json.dumps({"text": at_text})
            )
        if isinstance(seg, ImageSegment) and seg.data.url:
            return PlatformMessage(
                msg_type="image", content=json.dumps({"image_key": seg.data.url})
            )
        return None

    def _parse_segments(
        self,
        msg_type: str,
        content_json: str | None,
        mentions: list[Any] | None,
    ) -> list[MessageSegment]:
        if not content_json:
            return []

        try:
            content: dict[str, Any] = json.loads(content_json)
        except (json.JSONDecodeError, TypeError):
            return [TextSegment(data={"text": content_json})]

        segments: list[MessageSegment] = []

        if msg_type == "text":
            text: str = content.get("text", "")
            if mentions:
                for m in mentions:
                    placeholder: str | None = m.key
                    if not placeholder or placeholder not in text:
                        continue
                    before, _, after = text.partition(placeholder)
                    if before:
                        segments.append(TextSegment(data={"text": before}))
                    m_id = m.id
                    user_id = (m_id.open_id if m_id else None) or placeholder
                    segments.append(AtSegment(data=AtData(user_id=user_id, name=m.name or "")))
                    text = after
                if text:
                    segments.append(TextSegment(data={"text": text}))
            else:
                segments.append(TextSegment(data={"text": text}))

        elif msg_type == "image":
            image_key: str = content.get("image_key", "")
            segments.append(ImageSegment(data=ImageData(file=image_key, name=image_key)))

        elif msg_type == "post":
            title: str = content.get("title", "")
            if title:
                segments.append(TextSegment(data={"text": f"[{title}]\n"}))
            for lang_content in content.values():
                if isinstance(lang_content, list):
                    for line in lang_content:
                        for element in line:
                            tag: str = element.get("tag", "")
                            if tag == "text":
                                segments.append(
                                    TextSegment(data={"text": element.get("text", "")})
                                )
                            elif tag == "a":
                                segments.append(
                                    TextSegment(data={"text": element.get("href", "")})
                                )
                            elif tag == "at":
                                segments.append(
                                    AtSegment(
                                        data=AtData(
                                            user_id=element.get("user_id", ""),
                                            name=element.get("user_name", ""),
                                        )
                                    )
                                )
                            elif tag == "img":
                                segments.append(
                                    ImageSegment(
                                        data=ImageData(
                                            file=element.get("image_key", ""),
                                            name=element.get("image_key", ""),
                                        )
                                    )
                                )
                    break

        else:
            segments.append(TextSegment(data={"text": f"[{msg_type}]"}))

        return segments
