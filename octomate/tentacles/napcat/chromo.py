from __future__ import annotations

import json
import logging

from octomate.schemas.events import MessageEvent
from octomate.schemas.segments import AgentSegment, MarkdownSegment, TextSegment
from octomate.tentacles.base import PlatformMessage
from octomate.tentacles.napcat.schema import (
    ActionResponse,
    NapcatGroupMessageEvent,
    NapcatPrivateMessageEvent,
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
        if not isinstance(frame, (NapcatGroupMessageEvent, NapcatPrivateMessageEvent)):
            return None

        return to_message_event(frame)

    async def squirt(
        self, segments: list[AgentSegment], *, reply_to: str | None = None
    ) -> list[PlatformMessage]:
        resolved: list[AgentSegment] = []
        for seg in segments:
            if isinstance(seg, MarkdownSegment):
                resolved.append(
                    TextSegment(data={"text": strip_markdown(seg.data["text"])})
                )
            elif isinstance(seg, TextSegment):
                resolved.append(
                    TextSegment(data={"text": strip_markdown(seg.data["text"])})
                )
            else:
                resolved.append(seg)

        seg_list = [json.loads(s.model_dump_json()) for s in resolved]
        return [PlatformMessage(msg_type="onebot_message", content=json.dumps(seg_list))]
