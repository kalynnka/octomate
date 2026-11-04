"""Slack's assistant-thread status indicator."""

from __future__ import annotations

from html import escape

from octomate.schemas.conversation import ChannelAddress
from octomate.tentacles.feelers.status import StatusFeeler
from octomate.tentacles.slack.ink import SlackInk


class SlackStatusFeeler(StatusFeeler):
    def __init__(self, ink: SlackInk) -> None:
        super().__init__()
        self.ink = ink

    async def present(self, address: ChannelAddress, text: str) -> None:
        if address.channel_thread_id is None:
            return
        await self.ink.set_assistant_status(
            address.chat_id or address.user_id,
            address.channel_thread_id,
            escape(text, quote=False),
        )
