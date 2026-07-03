"""Lark channel fakes and builders.

`FakeLarkInk` is a real `LarkInk` subclass that records every outbound call, so
tests exercise the production send/stream routing. `FakeLarkCardsInk` is a bare
`send_message` recorder that keeps the outbound `LarkOutboundMessage` objects
intact for card-content assertions. `lark_channel` assembles a `LarkTentacle`
over a fake ink with real composed feelers.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from pydantic import SecretStr

from octomate.config import LarkChannelConfig, LarkStreamConfig
from octomate.managers.deferred import DeferredActionManager
from octomate.tentacles.channel.feelers.base import Feelers
from octomate.tentacles.channel.feelers.output import DefaultSegmentsFeeler
from octomate.tentacles.channel.lark import LarkChromo, LarkInk, LarkTentacle
from octomate.tentacles.channel.lark.feelers.approvals import LarkApprovalFeeler
from octomate.tentacles.channel.lark.feelers.output import (
    LarkMarkdownFeeler,
    LarkTimelineFeeler,
)
from octomate.tentacles.channel.lark.feelers.questions import LarkAskQuestionFeeler
from octomate.tentacles.channel.lark.schema import LarkOutboundMessage, LarkStreamCard


class FakeLarkInk(LarkInk):
    def __init__(self) -> None:
        self.created: list[tuple[str, str, str, str]] = []
        self.replies: list[tuple[str, str, str, bool]] = []
        self.stream_cards: list[tuple[str, str]] = []
        self.stream_messages: list[
            tuple[str, str, LarkStreamCard, str | None, bool]
        ] = []
        self.stream_updates: list[tuple[LarkStreamCard, str, int]] = []
        self.finalized: list[tuple[LarkStreamCard, int]] = []
        self.patched: list[tuple[str, str]] = []
        self.uploaded: list[bytes] = []
        self.fail_interactive_send = False
        self.fail_stream_create = False
        self.fail_stream_update = False

    async def upload_media(self, data: bytes) -> str | None:
        self.uploaded.append(data)
        return f"img-address-{len(self.uploaded)}"

    async def _create_message(
        self,
        receive_id: str,
        receive_id_type: str,
        msg_type: str,
        content: str,
    ) -> str | None:
        if self.fail_interactive_send and msg_type == "interactive":
            return None
        self.created.append((receive_id, receive_id_type, msg_type, content))
        return f"created-{len(self.created)}"

    async def reply_message(
        self,
        message_id: str,
        msg_type: str,
        content: str,
        *,
        reply_in_thread: bool = False,
    ) -> str | None:
        if self.fail_interactive_send and msg_type == "interactive":
            return None
        self.replies.append((message_id, msg_type, content, reply_in_thread))
        return f"reply-{len(self.replies)}"

    async def create_stream_card(
        self,
        card_data: str,
        *,
        element_id: str,
    ) -> LarkStreamCard:
        self.stream_cards.append((card_data, element_id))
        if self.fail_stream_create:
            raise RuntimeError("Lark create stream card failed: simulated")
        return LarkStreamCard(
            card_id=f"card-{len(self.stream_cards)}",
            element_id=element_id,
        )

    async def send_stream_card(
        self,
        chat_id: str,
        chat_type: str,
        card: LarkStreamCard,
        *,
        reply_to: str | None = None,
        reply_in_thread: bool = False,
    ) -> str | None:
        self.stream_messages.append(
            (chat_id, chat_type, card, reply_to, reply_in_thread)
        )
        return f"stream-{len(self.stream_messages)}"

    async def update_stream_card(
        self,
        card: LarkStreamCard,
        *,
        content: str,
        sequence: int,
    ) -> bool:
        self.stream_updates.append((card, content, sequence))
        return not self.fail_stream_update

    async def finish_stream_card(
        self,
        card: LarkStreamCard,
        *,
        sequence: int,
    ) -> bool:
        self.finalized.append((card, sequence))
        return True

    async def patch_card(self, message_id: str, content: str) -> bool:
        self.patched.append((message_id, content))
        return True


@dataclass
class FakeLarkCardsInk:
    sent: list[tuple[str, str, list[LarkOutboundMessage], str | None, bool]] = field(
        default_factory=list
    )

    async def send_message(
        self,
        chat_id: str,
        chat_type: str,
        messages: list[LarkOutboundMessage],
        reply_to: str | None = None,
        reply_in_thread: bool = False,
    ) -> str:
        self.sent.append((chat_id, chat_type, messages, reply_to, reply_in_thread))
        return f"lark-{len(self.sent)}"


def lark_channel(
    ink: FakeLarkInk, deferred_actions: DeferredActionManager | None = None
) -> LarkTentacle:
    channel = object.__new__(LarkTentacle)
    channel.id = "lark"
    channel.ink = ink
    channel.chromo = LarkChromo()
    channel.config = LarkChannelConfig(
        app_id="cli-test",
        app_secret=SecretStr("secret"),
        stream=LarkStreamConfig(flush_interval=0.2, min_chars=1),
    )
    compose_lark_feelers(channel, deferred_actions)
    return channel


def enable_lark_stream(channel: LarkTentacle, *, interval: float = 0.2) -> None:
    channel.config.stream = LarkStreamConfig(flush_interval=interval, min_chars=1)
    compose_lark_feelers(channel)


def compose_lark_feelers(
    channel: LarkTentacle, deferred_actions: DeferredActionManager | None = None
) -> None:
    ink = channel.ink
    chromo = channel.chromo
    markdown_feeler = LarkMarkdownFeeler(ink=ink, chromo=chromo)
    approvals = LarkApprovalFeeler(ink)
    ask_questions = LarkAskQuestionFeeler(ink)
    actions = deferred_actions or DeferredActionManager()
    channel.feelers = Feelers(
        markdown=markdown_feeler,
        timeline=LarkTimelineFeeler(
            ink=ink,
            chromo=chromo,
            stream_config=channel.config.stream,
            ask_questions=ask_questions,
            approvals=approvals,
            deferred_actions=actions,
        ),
        segments=DefaultSegmentsFeeler(ink=ink, chromo=chromo),
        approvals=approvals,
        ask_questions=ask_questions,
    )
