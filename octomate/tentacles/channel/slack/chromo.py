from __future__ import annotations

import json
import logging
import re
import time
from collections.abc import AsyncIterator
from dataclasses import asdict, is_dataclass
from typing import Any

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
from octomate.tentacles.channel.slack.schema import (
    SlackFileInfo,
    SlackMessageEvent,
    SlackOutboundMessage,
)

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
        events: AsyncIterator[AgentStreamEvent | AgentRunResultEvent[Any]],
        *,
        reply_to: str | None = None,
    ) -> AsyncIterator[SlackOutboundMessage]:
        async for event in events:
            if not isinstance(event, AgentRunResultEvent):
                continue
            text = self._render_result(event.result)
            if text:
                yield self._make_message(text)

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

    def _render_result(self, result: AgentRunResult[Any]) -> str:
        output = result.output
        if isinstance(output, DeferredToolRequests):
            return self._render_deferred(output)
        if isinstance(output, str):
            return output
        if output is None:
            return ""
        return self._render_structured(output)

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

    def _make_message(self, text: str) -> SlackOutboundMessage:
        mrkdwn = _md_to_mrkdwn(text)
        return SlackOutboundMessage(
            text=text,
            blocks=[
                {
                    "type": "section",
                    "text": {"type": "mrkdwn", "text": mrkdwn},
                }
            ],
        )


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
