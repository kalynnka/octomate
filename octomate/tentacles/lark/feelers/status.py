"""Lark activity as a compact card, withdrawn when its indicator clears."""

from __future__ import annotations

import json
from typing import ClassVar

from octomate.schemas.conversation import ChannelAddress
from octomate.schemas.thread import ThreadKey
from octomate.tentacles.feelers.status import StatusFeeler
from octomate.tentacles.lark.feelers import cards
from octomate.tentacles.lark.ink import LarkInk
from octomate.tentacles.lark.schema import LarkOutboundMessage


class LarkStatusFeeler(StatusFeeler):
    update_interval: ClassVar[float] = 1.0
    refresh_interval: ClassVar[float] = float("inf")

    def __init__(self, ink: LarkInk) -> None:
        super().__init__()
        self.ink = ink
        self.messages: dict[ThreadKey, str] = {}

    async def present(self, address: ChannelAddress, text: str) -> None:
        key = ThreadKey.from_address(address)
        message_id = self.messages.get(key)
        if not text:
            if message_id is not None:
                self.messages.pop(key)
                await self.ink.delete_message(message_id)
            return
        content = json.dumps(
            cards.simple_card(
                [{"tag": "div", "text": {"tag": "plain_text", "content": text}}]
            )
        )
        if message_id is not None:
            if not await self.ink.patch_card(message_id, content):
                raise RuntimeError("Lark status card update failed")
            return
        chat_id = address.chat_id or address.user_id
        sent = await self.ink.send_message(
            chat_id,
            address.chat_type,
            [LarkOutboundMessage(msg_type="interactive", content=content)],
            channel_thread_id=address.channel_thread_id or chat_id,
            reply_in_thread=address.chat_type == "thread",
        )
        if sent is not None:
            self.messages[key] = sent
