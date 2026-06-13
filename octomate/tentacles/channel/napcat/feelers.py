from __future__ import annotations

from octomate.schemas.conversation import ConversationKey
from octomate.schemas.segments import MessageSegment
from octomate.tentacles.channel.feelers.output import IMMessageID
from octomate.tentacles.channel.napcat.chromo import NapcatChromo
from octomate.tentacles.channel.napcat.ink import NapcatInk


class NapcatSegmentsFeeler:
    """Delivers output segments to NapCat natively: text/markdown flatten to
    OneBot text, image/file ship inline as `base64://` segments."""

    def __init__(self, *, ink: NapcatInk, chromo: NapcatChromo) -> None:
        self.ink = ink
        self.chromo = chromo

    async def present(
        self,
        key: ConversationKey,
        segments: list[MessageSegment],
    ) -> IMMessageID | None:
        messages = await self.chromo.outbound_segments(segments)
        if not messages:
            return None
        chat_id = key.chat_id or key.user_id
        return await self.ink.send_message(
            chat_id, key.chat_type, messages, key.thread_id or None
        )
