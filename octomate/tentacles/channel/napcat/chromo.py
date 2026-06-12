from __future__ import annotations

import logging

from octomate.schemas.events import MessageEvent
from octomate.tentacles.channel.base import Chromo
from octomate.tentacles.channel.napcat.schema import (
    ActionResponse,
    NapcatMessageEvent,
    NapcatOutboundMessage,
    inbound_adapter,
    to_message_event,
)
from octomate.utils import strip_markdown

logger = logging.getLogger(__name__)


class NapcatChromo(Chromo[str | bytes, NapcatOutboundMessage]):
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

    def outbound_markdown(self, text: str) -> list[NapcatOutboundMessage]:
        stripped = strip_markdown(text)
        if not stripped:
            return []
        return [
            NapcatOutboundMessage(segments=[{"type": "text", "data": {"text": stripped}}])
        ]
