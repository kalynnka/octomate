"""The OAuth feeler across channels: cards where the platform has them, plain
text where it does not, and the rule every channel shares about where a one-time
code may land. No channel finishes the connection from its own message — the user
says so in chat and the capability's confirm tool does it."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import cast

import anyio
import httpx
import pytest
from pydantic import AnyHttpUrl, JsonValue

from octomate.base import Octomate
from octomate.capabilities.harness.events import (
    LinkProfileAuthorizationEvent,
    OAuthAuthorizationEvent,
    OAuthDeviceAuthorizationEvent,
    wire_event_adapter,
)
from octomate.config.channels import NapcatChannelConfig, TrunklineChannelConfig
from octomate.schemas.auth import LinkProfileAuthorization
from octomate.schemas.conversation import ChannelAddress, ChatType
from octomate.schemas.user import UserProfile
from octomate.tentacles.channel import Ink
from octomate.tentacles.feelers.oauth import AuthorizationEvent, PlainTextOAuthFeeler
from octomate.tentacles.feelers.output import DefaultTimelineFeeler
from octomate.tentacles.lark.feelers.oauth import (
    LarkOAuthFeeler,
    authorization_card_data,
)
from octomate.tentacles.lark.ink import LarkInk
from octomate.tentacles.napcat.base import NapcatTentacle
from octomate.tentacles.napcat.feelers import NapcatOAuthFeeler
from octomate.tentacles.slack.feelers.actions import SlackBlockAction
from octomate.tentacles.slack.feelers.oauth import (
    SlackOAuthFeeler,
    authorization_blocks,
)
from octomate.tentacles.slack.ink import SlackInk
from octomate.tentacles.slack.schema import SlackOutboundMessage
from octomate.tentacles.trunkline.base import (
    TrunklineStreamItem,
    TrunklineTentacle,
    current_sink,
    to_wire,
)
from octomate.types.json import JsonObject
from tests.channels.lark.fakes import FakeLarkCardsInk
from tests.support.channels import FakeOAuthInk, RecordingInk, RecordingMarkdownFeeler

AUTHORIZATION = OAuthDeviceAuthorizationEvent(
    connector_id="github",
    label="GitHub",
    authorization_uri="https://github.com/login/device",
    user_code="ABCD-EFGH",
)

LINK_AUTHORIZATION = OAuthAuthorizationEvent(
    connector_id="linear",
    label="Linear",
    authorization_uri="http://127.0.0.1:8000/oauth/linear/start/test_one_two-three",
)

HOST_AUTHORIZATION = LinkProfileAuthorizationEvent(
    host="Octomate",
    authorization=LinkProfileAuthorization(
        profile=UserProfile(
            channel_tentacle_id="discord", channel_user_id="U1", name="Alice"
        ),
        expires_at=datetime.now(UTC) + timedelta(minutes=10),
        authorization_uri=AnyHttpUrl(
            "https://octomate.example/#link-profile=test_one_two-three"
        ),
    ),
)


@pytest.mark.parametrize(
    "event", [AUTHORIZATION, LINK_AUTHORIZATION, HOST_AUTHORIZATION]
)
@pytest.mark.parametrize("shared", [False, True])
async def test_napcat_authorizations_keep_literal_urls_in_private_messages(
    event: AuthorizationEvent, shared: bool
) -> None:
    channel = NapcatTentacle(
        "napcat",
        Octomate(),
        config=NapcatChannelConfig(
            agents=["agent"], ws_url="ws://napcat.test", http_url="http://napcat.test"
        ),
    )
    requests: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"data": {"message_id": "msg-1"}})

    await channel.ink.httpx.aclose()
    async with httpx.AsyncClient(
        base_url="http://napcat.test", transport=httpx.MockTransport(respond)
    ) as client:
        channel.ink.httpx = client
        assert isinstance(channel.feelers.oauth, NapcatOAuthFeeler)
        assert isinstance(channel.feelers.timeline, DefaultTimelineFeeler)
        assert channel.feelers.timeline.oauth is channel.feelers.oauth
        message_id = await channel.feelers.oauth.present(
            _address("napcat", "group" if shared else "dm", shared=shared), event
        )

    assert message_id == "msg-1"
    [request] = requests
    assert request.url.path == "/send_private_msg"
    payload = json.loads(request.content)
    assert payload["user_id"] == ("U1" if shared else "C1")
    assert "reply" not in payload
    [segment] = payload["message"]
    assert segment["type"] == "text"
    text = segment["data"]["text"]
    if isinstance(event, LinkProfileAuthorizationEvent):
        assert str(event.authorization.authorization_uri) in text
        assert "Octomate authorization" in text
        assert "Profile ID: U1" in text
    else:
        assert event.authorization_uri in text
    if isinstance(event, OAuthDeviceAuthorizationEvent):
        assert event.user_code in text


@pytest.mark.parametrize("event", [AUTHORIZATION, LINK_AUTHORIZATION])
async def test_trunkline_feeler_streams_authorization_without_markdown(
    event: OAuthAuthorizationEvent,
) -> None:
    channel = TrunklineTentacle(
        "web", Octomate(), config=TrunklineChannelConfig(agents=["agent"])
    )
    send, receive = anyio.create_memory_object_stream[TrunklineStreamItem](1)
    async with send, receive:
        token = current_sink.set(send)
        try:
            message_id = await channel.feelers.oauth.present(_address("web"), event)
        finally:
            current_sink.reset(token)
        assert message_id is not None
        item = await receive.receive()
        assert item is event
        wire = to_wire(item)
        assert wire is not None
        assert json.loads(wire_event_adapter.dump_json(wire)) == event.model_dump(
            mode="json"
        )


async def test_trunkline_authorization_requires_an_active_private_stream() -> None:
    channel = TrunklineTentacle(
        "web", Octomate(), config=TrunklineChannelConfig(agents=["agent"])
    )
    with pytest.raises(RuntimeError, match="active browser request"):
        await channel.feelers.oauth.present(_address("web"), LINK_AUTHORIZATION)

    send, receive = anyio.create_memory_object_stream[TrunklineStreamItem](1)
    async with send, receive:
        token = current_sink.set(send)
        try:
            with pytest.raises(RuntimeError, match="does not support profile linking"):
                await channel.feelers.oauth.present(_address("web"), HOST_AUTHORIZATION)
            with pytest.raises(RuntimeError, match="not going to a group"):
                await channel.feelers.oauth.present(
                    _address("web", "group", shared=True), LINK_AUTHORIZATION
                )
            with pytest.raises(anyio.WouldBlock):
                receive.receive_nowait()
            await receive.aclose()
            with pytest.raises(anyio.BrokenResourceError):
                await channel.feelers.oauth.present(_address("web"), LINK_AUTHORIZATION)
        finally:
            current_sink.reset(token)


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


def _address(
    channel: str,
    chat_type: ChatType = "dm",
    *,
    shared: bool = False,
    channel_thread_id: str | None = None,
) -> ChannelAddress:
    return ChannelAddress(
        channel_tentacle_id=channel,
        chat_type=chat_type,
        chat_id="C1",
        user_id="U1",
        channel_thread_id=channel_thread_id,
        shared=shared,
    )


@dataclass
class RecordingSlackInk:
    sent: list[tuple[str, str, list[SlackOutboundMessage], str | None, str]] = field(
        default_factory=list
    )
    # What `open_dm` answers, so a test says whether this platform has anywhere
    # private to reach the user; `opened` records who was asked for.
    dm_chat_id: str | None = None
    opened: list[str] = field(default_factory=list)

    async def open_dm(self, user_id: str, opener: str | None = None) -> str | None:
        self.opened.append(user_id)
        return self.dm_chat_id

    async def send_message(
        self,
        chat_id: str,
        chat_type: str,
        messages: list[SlackOutboundMessage],
        *,
        channel_thread_id: str,
        reply_to: str | None = None,
        reply_in_thread: bool = False,
    ) -> str:
        self.sent.append((chat_id, chat_type, messages, reply_to, channel_thread_id))
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
    [
        (
            _chat_id,
            _chat_type,
            messages,
            _reply_to,
            _in_thread,
            _channel_thread_id,
        )
    ] = ink.sent
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

    [(_chat_id, _chat_type, messages, _reply_to, _channel_thread_id)] = ink.sent
    blocks = _objs(cast(JsonValue, messages[0].blocks))
    assert "ABCD-EFGH" in _text(_obj(blocks[1]["text"])["text"])
    [open_button] = _objs(blocks[2]["elements"])
    assert open_button["url"] == "https://github.com/login/device"
    assert open_button["action_id"] == SlackBlockAction.OAUTH_OPEN.value
    assert "value" not in open_button


async def test_a_group_request_delivers_the_code_to_the_user_dm() -> None:
    ink = FakeLarkCardsInk(dm_chat_id="D1")

    await LarkOAuthFeeler(cast(LarkInk, ink)).present(
        _address("lark", "group", shared=True), AUTHORIZATION
    )

    # The code authorizes one person's account; the group it was asked from does
    # not get to read it.
    assert ink.opened == ["U1"]
    [
        (
            chat_id,
            chat_type,
            _messages,
            reply_to,
            _in_thread,
            channel_thread_id,
        )
    ] = ink.sent
    assert (chat_id, chat_type) == ("D1", "dm")
    assert reply_to is None
    assert channel_thread_id == "D1"


async def test_a_private_thread_keeps_the_code_where_it_was_asked_for() -> None:
    """A Slack assistant pane is a thread only its own user can read. Moving the code
    out of it would put the link in one surface and "return here and tell me to
    confirm" in another."""
    ink = RecordingSlackInk(dm_chat_id="D1")

    await SlackOAuthFeeler(cast(SlackInk, ink)).present(
        _address("slack", "thread", channel_thread_id="1700.1"), AUTHORIZATION
    )

    assert ink.opened == []
    [(chat_id, chat_type, _messages, reply_to, channel_thread_id)] = ink.sent
    assert (chat_id, chat_type, reply_to) == ("C1", "thread", None)
    assert channel_thread_id == "1700.1"


async def test_every_channel_routes_a_group_request_the_same_way() -> None:
    # The rule belongs to the base feeler's `present`, so a channel that renders
    # cards and one that renders text cannot disagree about where a code may land.
    slack = RecordingSlackInk(dm_chat_id="D1")
    markdown = RecordingMarkdownFeeler()
    plain_ink = FakeOAuthInk(dm_chat_id="D2")

    await SlackOAuthFeeler(cast(SlackInk, slack)).present(
        _address("slack", "group", shared=True), AUTHORIZATION
    )
    await PlainTextOAuthFeeler(cast(Ink[str], plain_ink), markdown).present(
        _address("napcat", "group", shared=True), AUTHORIZATION
    )

    assert slack.opened == ["U1"]
    [(chat_id, chat_type, _messages, _reply_to, _channel_thread_id)] = slack.sent
    assert (chat_id, chat_type) == ("D1", "dm")
    assert plain_ink.opened == ["U1"]
    [(address, _)] = markdown.calls
    assert (address.chat_id, address.chat_type) == ("D2", "dm")


async def test_a_platform_with_nowhere_private_refuses_to_use_the_group() -> None:
    ink = FakeLarkCardsInk()

    with pytest.raises(RuntimeError, match="not going to a group"):
        await LarkOAuthFeeler(cast(LarkInk, ink)).present(
            _address("lark", "group", shared=True), AUTHORIZATION
        )

    assert ink.sent == []


async def test_plain_text_feeler_asks_for_nothing_a_link_flow_cannot_give() -> None:
    markdown = RecordingMarkdownFeeler()

    await PlainTextOAuthFeeler(cast(Ink[str], FakeOAuthInk()), markdown).present(
        _address("napcat"), LINK_AUTHORIZATION
    )

    [(_, text)] = markdown.calls
    assert LINK_AUTHORIZATION.authorization_uri in text
    # No code to type and nothing to come back for: the callback finishes it.
    assert "Code:" not in text
    assert "confirm" not in text


async def test_lark_card_drops_the_code_line_for_a_link_flow() -> None:
    ink = FakeLarkCardsInk()

    await LarkOAuthFeeler(cast(LarkInk, ink)).present(
        _address("lark"), LINK_AUTHORIZATION
    )

    [
        (
            _chat_id,
            _chat_type,
            messages,
            _reply_to,
            _in_thread,
            _channel_thread_id,
        )
    ] = ink.sent
    card = _obj(json.loads(messages[0].content))
    assert _text(_obj(_obj(card["header"])["title"])["content"]) == "Linear OAuth"
    elements = _objs(card["elements"])
    [open_button] = _objs(elements[2]["actions"])
    assert open_button["url"] == LINK_AUTHORIZATION.authorization_uri


async def test_slack_blocks_drop_the_code_block_for_a_link_flow() -> None:
    ink = RecordingSlackInk()

    await SlackOAuthFeeler(cast(SlackInk, ink)).present(
        _address("slack"), LINK_AUTHORIZATION
    )

    [(_chat_id, _chat_type, messages, _reply_to, _channel_thread_id)] = ink.sent
    blocks = _objs(cast(JsonValue, messages[0].blocks))
    # Body then actions, with no code section wedged between them.
    assert len(blocks) == 2
    [open_button] = _objs(blocks[1]["elements"])
    assert open_button["url"] == LINK_AUTHORIZATION.authorization_uri


def test_host_authorization_is_a_distinct_event_with_profile() -> None:
    assert not isinstance(HOST_AUTHORIZATION, OAuthAuthorizationEvent)
    payload = HOST_AUTHORIZATION.model_dump(mode="json")
    assert payload["event_kind"] == "link_profile_authorization"
    assert "connector_id" not in payload
    assert payload["authorization"]["profile"]["channel_user_id"] == "U1"
    assert set(payload["authorization"]) == {
        "profile",
        "expires_at",
        "authorization_uri",
    }
    restored = LinkProfileAuthorizationEvent.model_validate_json(
        HOST_AUTHORIZATION.model_dump_json()
    )
    assert (
        restored.authorization.profile.id == HOST_AUTHORIZATION.authorization.profile.id
    )
    assert (
        restored.authorization.authorization_uri
        == HOST_AUTHORIZATION.authorization.authorization_uri
    )


async def test_plain_text_host_authorization_carries_the_request() -> None:
    markdown = RecordingMarkdownFeeler()
    await PlainTextOAuthFeeler(RecordingInk(), markdown).present(
        _address("discord"), HOST_AUTHORIZATION
    )
    [(_, body)] = markdown.calls
    assert str(HOST_AUTHORIZATION.authorization.authorization_uri) in body
    assert "Requested through: discord" in body
    assert "Profile ID: U1" in body
    assert "Link this profile to your Octomate account" in body
    assert "return here" not in body


def test_slack_host_authorization_card_carries_the_request() -> None:
    [section, actions] = authorization_blocks(HOST_AUTHORIZATION)
    body = _text(_obj(section["text"])["text"])
    assert "Octomate authorization" in body
    assert "Profile ID: U1" in body
    assert "Link this profile to your Octomate account" in body
    [button] = _objs(actions["elements"])
    assert _obj(button["text"])["text"] == "Continue in Octomate"
    assert button["url"] == str(HOST_AUTHORIZATION.authorization.authorization_uri)


def test_lark_host_authorization_card_carries_the_request() -> None:
    card = authorization_card_data(HOST_AUTHORIZATION)
    assert _obj(_obj(card["header"])["title"])["content"] == "Octomate authorization"
    elements = _objs(card["elements"])
    body = _text(elements[0]["content"])
    assert "Profile ID: U1" in body
    assert "Link this profile to your Octomate account" in body
    [button] = _objs(elements[2]["actions"])
    assert _obj(button["text"])["content"] == "Continue in Octomate"
    assert button["url"] == str(HOST_AUTHORIZATION.authorization.authorization_uri)
