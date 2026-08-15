from __future__ import annotations

import json
import logging

import lark_oapi
from lark_oapi.api.im.v1.model.mention_event import MentionEvent
from lark_oapi.api.im.v1.model.p2_im_message_receive_v1 import P2ImMessageReceiveV1
from pydantic import TypeAdapter, ValidationError

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
from octomate.tentacles.channels.base import Chromo
from octomate.tentacles.channels.lark.schema import LarkOutboundMessage
from octomate.types.conversations import ChatType
from octomate.types.json import JsonObject

logger = logging.getLogger(__name__)
JsonObjectAdapter = TypeAdapter(JsonObject)

LARK_STREAM_ELEMENT_ID = "octomate_answer"


class LarkChromo(Chromo[P2ImMessageReceiveV1, LarkOutboundMessage]):
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

            segments = self.parse_segments(msg_type, message.content, message.mentions)

            reply_id = message.parent_id or ""
            if reply_id:
                segments.insert(0, ReplySegment(data={"id": reply_id}))

            # A reply inside a Lark thread arrives with thread_id set to the
            # thread ("omt_…") while root_id points at the thread's root message
            # ("om_…", what start_sub_thread keyed the sub-thread on). Key on the
            # root so continuations map to the same conversation and the feeler
            # can reply back into the thread; a non-threaded message has none.
            thread_id = None
            if message.thread_id:
                thread_id = message.root_id or message.thread_id

            sender_id_obj = event.sender.sender_id
            sender_id = (sender_id_obj.open_id or "") if sender_id_obj else ""

            lark_chat_type: ChatType
            if chat_type == "group":
                lark_chat_type = "group"
                chat_id = message.chat_id or ""
            elif chat_type == "p2p":
                lark_chat_type = "dm"
                chat_id = sender_id
            else:
                logger.warning("LarkChromo: unsupported chat_type %r", chat_type)
                return None
            if thread_id:
                lark_chat_type = "thread"

            return MessageEvent(
                message_id=message.message_id or "",
                channel_thread_id=thread_id,
                reply_id=reply_id,
                # Lark dates the message in milliseconds; 0.0 leaves `record_inbound`
                # to fall back to the moment it arrived.
                timestamp=int(message.create_time) / 1000
                if message.create_time
                else 0.0,
                user_id=sender_id,
                chat_id=chat_id,
                chat_type=lark_chat_type,
                # A topic reply promotes `lark_chat_type` to a thread whether the chat
                # around it is a group or a p2p, so the surface is read from Lark's own
                # word for it rather than from what the promotion left behind.
                shared=chat_type == "group",
                segments=segments,
                # The whole event, as Slack keeps it: `message.content` alone drops the
                # chat type, the topic ids and the mentions a decode is judged on.
                raw=lark_oapi.JSON.marshal(raw) or "",
            )
        except Exception:
            logger.warning("LarkChromo: failed to decode event", exc_info=True)
            return None

    def outbound_markdown(self, text: str) -> list[LarkOutboundMessage]:
        if not text:
            return []
        payload = {
            "schema": "2.0",
            "body": {"elements": [{"tag": "markdown", "content": text}]},
        }
        return [
            LarkOutboundMessage(msg_type="interactive", content=json.dumps(payload))
        ]

    async def outbound_segments(
        self, segments: list[MessageSegment]
    ) -> list[LarkOutboundMessage]:
        # `<at id=…></at>` is the markup Lark renders as a real mention in card
        # markdown; everything else flattens to its markdown text form.
        return self.outbound_markdown(
            "\n\n".join(
                f"<at id={seg.data.user_id}></at>"
                if isinstance(seg, AtSegment)
                else str(seg)
                for seg in segments
            )
        )

    def make_stream_card_data(
        self,
        text: str = "",
        *,
        element_id: str = LARK_STREAM_ELEMENT_ID,
    ) -> str:
        payload = {
            "schema": "2.0",
            "config": {
                "streaming_mode": True,
                "summary": {"content": ""},
                "streaming_config": {
                    "print_frequency_ms": {
                        "default": 20,
                        "android": 20,
                        "ios": 20,
                        "pc": 20,
                    },
                    "print_step": {
                        "default": 12,
                        "android": 12,
                        "ios": 12,
                        "pc": 12,
                    },
                    "print_strategy": "fast",
                },
            },
            "body": {
                "elements": [
                    {
                        "tag": "markdown",
                        "content": text,
                        "element_id": element_id,
                    }
                ]
            },
        }
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))

    def make_stream_card_message(self, card_id: str) -> LarkOutboundMessage:
        content = {"type": "card", "data": {"card_id": card_id}}
        return LarkOutboundMessage(
            msg_type="interactive",
            content=json.dumps(content, ensure_ascii=False, separators=(",", ":")),
        )

    def parse_segments(
        self,
        msg_type: str,
        content_json: str | None,
        mentions: list[MentionEvent] | None,
    ) -> list[MessageSegment]:
        if not content_json:
            return []

        try:
            content = JsonObjectAdapter.validate_json(content_json)
        except (ValidationError, TypeError):
            return [TextSegment(data={"text": content_json})]

        segments: list[MessageSegment] = []
        if msg_type == "text":
            raw_text = content.get("text", "")
            text = raw_text if isinstance(raw_text, str) else ""
            if mentions:
                for mention in mentions:
                    placeholder = mention.key
                    if not placeholder or placeholder not in text:
                        continue
                    before, _, after = text.partition(placeholder)
                    if before:
                        segments.append(TextSegment(data={"text": before}))
                    mention_id = mention.id
                    user_id = (
                        mention_id.open_id if mention_id else None
                    ) or placeholder
                    segments.append(
                        AtSegment(data=AtData(user_id=user_id, name=mention.name or ""))
                    )
                    text = after
            if text:
                segments.append(TextSegment(data={"text": text}))
        elif msg_type == "image":
            raw_image_key = content.get("image_key", "")
            image_key = raw_image_key if isinstance(raw_image_key, str) else ""
            segments.append(
                ImageSegment(data=ImageData(file=image_key, name=image_key))
            )
        elif msg_type == "post":
            raw_title = content.get("title", "")
            title = raw_title if isinstance(raw_title, str) else ""
            if title:
                segments.append(TextSegment(data={"text": f"[{title}]\n"}))
            for lang_content in content.values():
                if not isinstance(lang_content, list):
                    continue
                for line in lang_content:
                    if not isinstance(line, list):
                        continue
                    for element in line:
                        if not isinstance(element, dict):
                            continue
                        tag = element.get("tag", "")
                        if tag == "text":
                            text = element.get("text", "")
                            segments.append(
                                TextSegment(
                                    data={"text": text if isinstance(text, str) else ""}
                                )
                            )
                        elif tag == "a":
                            href = element.get("href", "")
                            segments.append(
                                TextSegment(
                                    data={"text": href if isinstance(href, str) else ""}
                                )
                            )
                        elif tag == "at":
                            user_id = element.get("user_id", "")
                            user_name = element.get("user_name", "")
                            segments.append(
                                AtSegment(
                                    data=AtData(
                                        user_id=(
                                            user_id if isinstance(user_id, str) else ""
                                        ),
                                        name=(
                                            user_name
                                            if isinstance(user_name, str)
                                            else ""
                                        ),
                                    )
                                )
                            )
                        elif tag == "img":
                            raw_image_key = element.get("image_key", "")
                            image_key = (
                                raw_image_key if isinstance(raw_image_key, str) else ""
                            )
                            segments.append(
                                ImageSegment(
                                    data=ImageData(file=image_key, name=image_key)
                                )
                            )
                break
        else:
            segments.append(TextSegment(data={"text": f"[{msg_type}]"}))
        return segments
