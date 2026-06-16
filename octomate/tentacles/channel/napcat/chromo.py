from __future__ import annotations

import base64
import logging
from pathlib import Path

import anyio

from octomate.schemas.events import MessageEvent
from octomate.schemas.segments import (
    AtSegment,
    FileSegment,
    ImageSegment,
    MarkdownSegment,
    MessageSegment,
    TextSegment,
)
from octomate.tentacles.channel.base import Chromo
from octomate.tentacles.channel.napcat.schema import (
    ActionResponse,
    NapcatMessageEvent,
    NapcatOutboundMessage,
    inbound_adapter,
    to_message_event,
)
from octomate.types.json import JsonObject
from octomate.utils import strip_markdown

logger = logging.getLogger(__name__)


async def inline_base64(path: Path) -> str:
    """Read a local file and encode it as a NapCat `base64://` reference so the
    OneBot side uploads it inline. Missing files raise (fail-fast)."""
    data = await anyio.Path(path).read_bytes()
    return f"base64://{base64.b64encode(data).decode()}"


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

    async def outbound_segments(
        self, segments: list[MessageSegment]
    ) -> list[NapcatOutboundMessage]:
        """Convert output segments to a single OneBot message. octomate's segment
        shape (`{"type", "data"}`) is OneBot's: text/markdown flatten through
        `strip_markdown`, image/file rewrite `data.file` to an inline `base64://`
        reference, and anything else (e.g. a card) degrades to its text form."""
        onebot: list[JsonObject] = []
        for seg in segments:
            match seg:
                case TextSegment() | MarkdownSegment():
                    text = strip_markdown(seg.data["text"])
                    if text:
                        onebot.append({"type": "text", "data": {"text": text}})
                case AtSegment():
                    onebot.append({"type": "at", "data": {"qq": seg.data.user_id}})
                case ImageSegment():
                    file = await inline_base64(seg.data.path)
                    onebot.append({"type": "image", "data": {"file": file}})
                case FileSegment():
                    data: JsonObject = {"file": await inline_base64(Path(seg.data.file))}
                    if seg.data.name:
                        data["name"] = seg.data.name
                    onebot.append({"type": "file", "data": data})
                case _:
                    text = str(seg)
                    if text:
                        onebot.append({"type": "text", "data": {"text": text}})
        if not onebot:
            return []
        return [NapcatOutboundMessage(segments=onebot)]
