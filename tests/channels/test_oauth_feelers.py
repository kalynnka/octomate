"""The OAuth feeler across channels: cards where the platform has them, plain
text where it does not, and the rule every channel shares about where a one-time
code may land. No channel finishes the connection from its own message — the user
says so in chat and the capability's confirm tool does it."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import cast

import pytest
from pydantic import JsonValue

from octomate.capabilities.harness.events import OAuthAuthorizationEvent
from octomate.schemas.conversation import ChannelAddress, ChatType
from octomate.tentacles.channel.base import Ink
from octomate.tentacles.channel.feelers.oauth import PlainTextOAuthFeeler
from octomate.tentacles.channel.lark.feelers.oauth import LarkOAuthFeeler
from octomate.tentacles.channel.lark.ink import LarkInk
from octomate.tentacles.channel.slack.feelers.actions import SlackBlockAction
from octomate.tentacles.channel.slack.feelers.oauth import SlackOAuthFeeler
from octomate.tentacles.channel.slack.ink import SlackInk
from octomate.tentacles.channel.slack.schema import SlackOutboundMessage
from octomate.types.json import JsonObject
from tests.channels.lark.fakes import FakeLarkCardsInk
from tests.support.channels import FakeOAuthInk, RecordingMarkdownFeeler

AUTHORIZATION = OAuthAuthorizationEvent(
    connector_id="github",
    label="GitHub",
    verification_uri="https://github.com/login/device",
    user_code="ABCD-EFGH",
)


def _obj(value: JsonValue) -> JsonObject:
    assert isinstance(value, dict)
    return value


def _objs(value: JsonValue) -> list[JsonObject]:
    assert isinstance(value, list)
    objects: list[JsonObject] = []
    for item in value:
        assert isinstance(item, dict)
        objects.append(item)
    return objects


def _text(value: JsonValue) -> str:
    assert isinstance(value, str)
    return value


def _address(channel: str, chat_type: ChatType = "private") -> ChannelAddress:
    return ChannelAddress(
        channel_tentacle_id=channel,
        chat_type=chat_type,
        chat_id="C1",
        user_id="U1",
        thread_id="",
    )


@dataclass
class RecordingSlackInk:
    sent: list[tuple[str, str, list[SlackOutboundMessage], str | None]] = field(
        default_factory=list
    )

    async def send_message(
        self,
        chat_id: str,
        chat_type: str,
        messages: list[SlackOutboundMessage],
        thread_ts: str | None = None,
    ) -> str:
        self.sent.append((chat_id, chat_type, messages, thread_ts))
        return f"slack-{len(self.sent)}"


async def test_plain_text_feeler_sends_the_link_and_code() -> None:
    markdown = RecordingMarkdownFeeler()

    await PlainTextOAuthFeeler(cast(Ink[str], FakeOAuthInk()), markdown).present(
        _address("napcat"), AUTHORIZATION
    )

    [(address, text)] = markdown.calls
    assert address.channel_tentacle_id == "napcat"
    assert "https://github.com/login/device" in text
    assert "ABCD-EFGH" in text
    # No button to press here, so the message has to say what to do instead.
    assert "confirm" in text


async def test_lark_feeler_sends_a_card_carrying_the_authorization() -> None:
    ink = FakeLarkCardsInk()

    message_id = await LarkOAuthFeeler(cast(LarkInk, ink)).present(
        _address("lark"), AUTHORIZATION
    )

    assert message_id == "lark-1"
    [(_chat_id, _chat_type, messages, _reply_to, _in_thread)] = ink.sent
    card = _obj(json.loads(messages[0].content))
    assert (
        _text(_obj(_obj(card["header"])["title"])["content"]) == "GitHub Device OAuth"
    )
    elements = _objs(card["elements"])
    # Lark card markdown has no code span, so a backticked code would show its
    # backticks to the user.
    assert "ABCD-EFGH" in _text(elements[0]["content"])
    assert "`" not in _text(elements[0]["content"])
    # Lark reads a link button's target from the element, not from `value`, and
    # rejects the card outright when it is missing. Nothing posts back from this
    # card, so it carries no state at all.
    [open_button] = _objs(elements[2]["actions"])
    assert open_button["url"] == "https://github.com/login/device"
    assert "value" not in open_button


async def test_slack_feeler_sends_blocks_carrying_the_authorization() -> None:
    ink = RecordingSlackInk()

    await SlackOAuthFeeler(cast(SlackInk, ink)).present(
        _address("slack"), AUTHORIZATION
    )

    [(_chat_id, _chat_type, messages, _thread)] = ink.sent
    blocks = _objs(cast(JsonValue, messages[0].blocks))
    assert "ABCD-EFGH" in _text(_obj(blocks[1]["text"])["text"])
    [open_button] = _objs(blocks[2]["elements"])
    assert open_button["url"] == "https://github.com/login/device"
    assert open_button["action_id"] == SlackBlockAction.OAUTH_OPEN.value
    assert "value" not in open_button


async def test_a_group_request_delivers_the_code_to_the_user_dm() -> None:
    ink = FakeLarkCardsInk(dm_chat_id="D1")

    await LarkOAuthFeeler(cast(LarkInk, ink)).present(
        _address("lark", "group"), AUTHORIZATION
    )

    # The code authorizes one person's account; the group it was asked from does
    # not get to read it.
    assert ink.opened == ["U1"]
    [(chat_id, chat_type, _messages, reply_to, _in_thread)] = ink.sent
    assert (chat_id, chat_type) == ("D1", "private")
    assert reply_to is None


async def test_a_platform_with_nowhere_private_refuses_to_use_the_group() -> None:
    ink = FakeLarkCardsInk()

    with pytest.raises(RuntimeError, match="not going to a group"):
        await LarkOAuthFeeler(cast(LarkInk, ink)).present(
            _address("lark", "group"), AUTHORIZATION
        )

    assert ink.sent == []
