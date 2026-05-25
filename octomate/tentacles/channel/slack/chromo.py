from __future__ import annotations

import json
import logging
import re
import time
from typing import Any

from pydantic_core import to_json
from pydantic_ai import AgentRunResult, AgentRunResultEvent, AgentStreamEvent
from pydantic_ai.messages import (
    PartDeltaEvent,
    PartStartEvent,
    TextPart,
    TextPartDelta,
)
from pydantic_ai.tools import DeferredToolRequests

from octomate.schemas.conversation import ConversationKey
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
from octomate.tentacles.channel.slack.schema import (
    SlackMessageEvent,
    SlackOutboundMessage,
    SlackThreadContext,
)

logger = logging.getLogger(__name__)

AT_RE = re.compile(r"<@(U[A-Z0-9]+)>")


class SlackChromo:
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

    def squirt(
        self,
        result: AgentRunResult[Any],
        *,
        reply_to: str | None = None,
    ) -> list[SlackOutboundMessage]:
        output = result.output
        if isinstance(output, DeferredToolRequests):
            lines: list[str] = ["Deferred tool requests:"]
            for call in output.calls:
                lines.append(
                    f"- `{call.tool_name}` needs input "
                    f"(`{call.tool_call_id}`): "
                    f"`{to_json(call.args_as_dict(), ensure_ascii=False, fallback=str).decode()}`"
                )
            for call in output.approvals:
                lines.append(
                    f"- `{call.tool_name}` needs approval "
                    f"(`{call.tool_call_id}`): "
                    f"`{to_json(call.args_as_dict(), ensure_ascii=False, fallback=str).decode()}`"
                )
            text = "\n".join(lines)
        elif isinstance(output, str):
            text = output
        elif output is None:
            return []
        else:
            payload = to_json(output, indent=2, ensure_ascii=False, fallback=str).decode()
            text = f"```json\n{payload}\n```"
        return [SlackOutboundMessage(text=text, markdown_text=text)] if text else []

    def thread_context(
        self,
        key: ConversationKey,
        source_events: list[MessageEvent] | None,
    ) -> SlackThreadContext:
        for event in reversed(source_events or ()):
            raw: dict[str, Any] = {}
            if event.raw:
                try:
                    maybe_raw = json.loads(event.raw)
                except json.JSONDecodeError:
                    maybe_raw = {}
                if isinstance(maybe_raw, dict):
                    raw = maybe_raw
            thread_ts = raw.get("thread_ts") or raw.get("ts")
            if not thread_ts:
                thread_ts = event.thread_id or event.message_id
            if thread_ts:
                return SlackThreadContext(
                    thread_ts=str(thread_ts),
                    recipient_user_id=str(raw.get("user") or event.user_id or "")
                    or None,
                    recipient_team_id=str(
                        raw.get("team")
                        or raw.get("team_id")
                        or raw.get("enterprise_id")
                        or ""
                    )
                    or None,
                )

        return SlackThreadContext(
            thread_ts=key.thread_id,
            recipient_user_id=key.user_id or None,
        )

    def render_stream_delta(
        self,
        event: AgentStreamEvent | AgentRunResultEvent[Any],
    ) -> str:
        if isinstance(event, PartStartEvent) and isinstance(event.part, TextPart):
            return event.part.content
        if isinstance(event, PartDeltaEvent) and isinstance(
            event.delta,
            TextPartDelta,
        ):
            return event.delta.content_delta
        return ""
