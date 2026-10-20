"""Lark artifact cards linking to the authenticated web viewer."""

from __future__ import annotations

from typing import TYPE_CHECKING

from octomate.schemas.artifacts import ArtifactOverview
from octomate.schemas.conversation import ChannelAddress
from octomate.telemetry import lark_logfire
from octomate.tentacles.channel import Ink
from octomate.tentacles.feelers.artifacts import ArtifactFeeler
from octomate.tentacles.feelers.output import IMMessageID
from octomate.tentacles.lark.feelers import cards
from octomate.tentacles.lark.schema import LarkInteractiveCard, LarkOutboundMessage

if TYPE_CHECKING:
    from octomate.managers.artifacts import ArtifactManager
    from octomate.managers.user import UserManager


class LarkArtifactFeeler(ArtifactFeeler):
    """Show a blue title header, provider subtitle, and a native viewer button.

    Titles and provider names remain plain text. The JSON 2.0 button opens the
    viewer directly, without a card callback or another upstream tool call.
    """

    def __init__(
        self,
        *,
        artifacts: ArtifactManager,
        users: UserManager,
        ink: Ink[LarkOutboundMessage],
    ) -> None:
        super().__init__(artifacts=artifacts, users=users)
        self.ink: Ink[LarkOutboundMessage] = ink

    @lark_logfire.instrument("lark.artifact.send", extract_args=False)
    async def send(
        self, address: ChannelAddress, overview: ArtifactOverview
    ) -> IMMessageID | None:
        header = cards.header(overview.title, template="blue")
        header["subtitle"] = {
            "tag": "plain_text",
            "content": f"{overview.provider_name} · Interactive artifact",
        }
        button = cards.button("Open artifact", button_type="primary")
        button["behaviors"] = [
            {"type": "open_url", "default_url": str(overview.viewer_url)}
        ]
        card = LarkInteractiveCard.model_validate(
            cards.card_v2([button], header=header)
        )
        chat_id = address.chat_id or address.user_id
        return await self.ink.send_message(
            chat_id,
            address.chat_type,
            [
                LarkOutboundMessage(
                    msg_type="interactive",
                    content=card.model_dump_json(by_alias=True, exclude_unset=True),
                )
            ],
            channel_thread_id=address.channel_thread_id or chat_id,
            reply_in_thread=address.chat_type == "thread",
        )
