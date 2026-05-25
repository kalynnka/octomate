from __future__ import annotations

import json
import logging
import time
from collections.abc import AsyncIterator
from dataclasses import asdict, is_dataclass
from typing import Any, Literal

from lark_oapi.api.im.v1.model.p2_im_message_receive_v1 import P2ImMessageReceiveV1
from pydantic import BaseModel
from pydantic_ai import AgentRunResult, AgentRunResultEvent, AgentStreamEvent
from pydantic_ai.tools import DeferredToolRequests

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
from octomate.tentacles.channel.lark.schema import LarkOutboundMessage

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

            reply_id = message.parent_id or ""
            if reply_id:
                segments.insert(0, ReplySegment(data={"id": reply_id}))

            sender_id_obj = event.sender.sender_id
            sender_id = (sender_id_obj.open_id or "") if sender_id_obj else ""

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
                thread_id=message.thread_id or "",
                reply_id=reply_id,
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
        self,
        events: AsyncIterator[AgentStreamEvent | AgentRunResultEvent[Any]],
        *,
        reply_to: str | None = None,
    ) -> AsyncIterator[LarkOutboundMessage]:
        async for event in events:
            if not isinstance(event, AgentRunResultEvent):
                continue
            text = self.render_result(event.result)
            if text:
                yield self.make_markdown_message(text)

    def render_result(self, result: AgentRunResult[Any]) -> str:
        output = result.output
        if isinstance(output, DeferredToolRequests):
            return self._render_deferred(output)
        if isinstance(output, str):
            return output
        if output is None:
            return ""
        return self._render_structured(output)

    def make_markdown_message(self, text: str) -> LarkOutboundMessage:
        payload = {
            "schema": "2.0",
            "body": {"elements": [{"tag": "markdown", "content": text}]},
        }
        return LarkOutboundMessage(msg_type="interactive", content=json.dumps(payload))

    def _render_deferred(self, requests: DeferredToolRequests) -> str:
        lines: list[str] = ["Deferred tool requests:"]
        for call in requests.calls:
            lines.append(
                f"- `{call.tool_name}` needs input "
                f"(`{call.tool_call_id}`): `{self._json_inline(call.args_as_dict())}`"
            )
        for call in requests.approvals:
            lines.append(
                f"- `{call.tool_name}` needs approval "
                f"(`{call.tool_call_id}`): `{self._json_inline(call.args_as_dict())}`"
            )
        return "\n".join(lines)

    def _render_structured(self, output: Any) -> str:
        payload = json.dumps(_jsonable(output), ensure_ascii=False, indent=2)
        return f"```json\n{payload}\n```"

    def _json_inline(self, value: Any) -> str:
        return json.dumps(_jsonable(value), ensure_ascii=False, default=str)

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
            text = content.get("text", "")
            if mentions:
                for mention in mentions:
                    placeholder = mention.key
                    if not placeholder or placeholder not in text:
                        continue
                    before, _, after = text.partition(placeholder)
                    if before:
                        segments.append(TextSegment(data={"text": before}))
                    mention_id = mention.id
                    user_id = (mention_id.open_id if mention_id else None) or placeholder
                    segments.append(
                        AtSegment(
                            data=AtData(user_id=user_id, name=mention.name or "")
                        )
                    )
                    text = after
            if text:
                segments.append(TextSegment(data={"text": text}))
        elif msg_type == "image":
            image_key = content.get("image_key", "")
            segments.append(ImageSegment(data=ImageData(file=image_key, name=image_key)))
        elif msg_type == "post":
            self._parse_post_segments(content, segments)
        else:
            segments.append(TextSegment(data={"text": f"[{msg_type}]"}))
        return segments

    def _parse_post_segments(
        self, content: dict[str, Any], segments: list[MessageSegment]
    ) -> None:
        title = content.get("title", "")
        if title:
            segments.append(TextSegment(data={"text": f"[{title}]\n"}))
        for lang_content in content.values():
            if not isinstance(lang_content, list):
                continue
            for line in lang_content:
                for element in line:
                    tag = element.get("tag", "")
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
                        image_key = element.get("image_key", "")
                        segments.append(
                            ImageSegment(data=ImageData(file=image_key, name=image_key))
                        )
            break


def _jsonable(value: Any) -> Any:
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    if is_dataclass(value) and not isinstance(value, type):
        return asdict(value)
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_jsonable(item) for item in value]
    try:
        json.dumps(value)
    except TypeError:
        return str(value)
    return value
