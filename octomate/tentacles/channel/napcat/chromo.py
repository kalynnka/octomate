from __future__ import annotations

import logging
from typing import Any

from pydantic_core import to_json
from pydantic_ai import AgentRunResult
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

    def squirt(
        self,
        result: AgentRunResult[Any],
        *,
        reply_to: str | None = None,
    ) -> list[NapcatOutboundMessage]:
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
            text = "\n".join(lines)
        elif isinstance(output, str):
            text = strip_markdown(output)
        elif output is None:
            return []
        else:
            payload = to_json(output, indent=2, ensure_ascii=False, fallback=str).decode()
            text = strip_markdown(f"```json\n{payload}\n```")
        return [self.make_text_message(text)] if text else []

    def make_text_message(self, text: str) -> NapcatOutboundMessage:
        return NapcatOutboundMessage(
            segments=[{"type": "text", "data": {"text": text}}]
        )
