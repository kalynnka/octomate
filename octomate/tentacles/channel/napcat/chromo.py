from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from typing import Any

from pydantic_core import to_json
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
            lines: list[str] = ["Deferred tool requests:"]
            for call in output.calls:
                lines.append(
                    f"- {call.tool_name} needs input "
                    f"({call.tool_call_id}): "
                    f"{to_json(call.args_as_dict(), ensure_ascii=False, fallback=str).decode()}"
                )
            for call in output.approvals:
                lines.append(
                    f"- {call.tool_name} needs approval "
                    f"({call.tool_call_id}): "
                    f"{to_json(call.args_as_dict(), ensure_ascii=False, fallback=str).decode()}"
                )
            return "\n".join(lines)
        if isinstance(output, str):
            return strip_markdown(output)
        if output is None:
            return ""
        payload = to_json(output, indent=2, ensure_ascii=False, fallback=str).decode()
        return strip_markdown(f"```json\n{payload}\n```")

    def make_text_message(self, text: str) -> NapcatOutboundMessage:
        return NapcatOutboundMessage(
            segments=[{"type": "text", "data": {"text": text}}]
        )
