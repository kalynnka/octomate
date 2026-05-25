from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator
from dataclasses import asdict, is_dataclass
from typing import Any

from pydantic import BaseModel
from pydantic_ai import AgentRunResult, AgentRunResultEvent, AgentStreamEvent
from pydantic_ai.tools import DeferredToolRequests

from octomate.schemas.events import MessageEvent
from octomate.tentacles.channel.napcat.schema import (
    ActionResponse,
    NapcatMessageEvent,
    NapcatOutboundMessage,
    inbound_adapter,
    to_message_event,
)
from octomate.utils import strip_markdown

logger = logging.getLogger(__name__)


class NapcatChromo:
    async def sip(self, raw: str | bytes) -> MessageEvent | None:
        try:
            frame = inbound_adapter.validate_json(raw)
        except Exception:
            logger.warning("NapcatChromo: failed to parse frame", exc_info=True)
            return None

        if isinstance(frame, ActionResponse):
            return None
        if not isinstance(frame, NapcatMessageEvent):
            return None
        return to_message_event(frame)

    async def squirt(
        self,
        events: AsyncIterator[AgentStreamEvent | AgentRunResultEvent[Any]],
        *,
        reply_to: str | None = None,
    ) -> AsyncIterator[NapcatOutboundMessage]:
        async for event in events:
            if not isinstance(event, AgentRunResultEvent):
                continue
            text = self.render_result(event.result)
            if text:
                yield self.make_text_message(text)

    def render_result(self, result: AgentRunResult[Any]) -> str:
        output = result.output
        if isinstance(output, DeferredToolRequests):
            return self._render_deferred(output)
        if isinstance(output, str):
            return strip_markdown(output)
        if output is None:
            return ""
        return strip_markdown(self._render_structured(output))

    def make_text_message(self, text: str) -> NapcatOutboundMessage:
        return NapcatOutboundMessage(
            segments=[{"type": "text", "data": {"text": text}}]
        )

    def _render_deferred(self, requests: DeferredToolRequests) -> str:
        lines: list[str] = ["Deferred tool requests:"]
        for call in requests.calls:
            lines.append(
                f"- {call.tool_name} needs input "
                f"({call.tool_call_id}): {self._json_inline(call.args_as_dict())}"
            )
        for call in requests.approvals:
            lines.append(
                f"- {call.tool_name} needs approval "
                f"({call.tool_call_id}): {self._json_inline(call.args_as_dict())}"
            )
        return "\n".join(lines)

    def _render_structured(self, output: Any) -> str:
        payload = json.dumps(_jsonable(output), ensure_ascii=False, indent=2)
        return f"```json\n{payload}\n```"

    def _json_inline(self, value: Any) -> str:
        return json.dumps(_jsonable(value), ensure_ascii=False, default=str)


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
