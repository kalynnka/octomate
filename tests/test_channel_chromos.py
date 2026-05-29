from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any
from typing import cast

from lark_oapi.api.im.v1.model.p2_im_message_receive_v1 import P2ImMessageReceiveV1
from pydantic import BaseModel, SecretStr
from pydantic_ai import AgentRunResult, AgentRunResultEvent
from pydantic_ai.messages import (
    AgentStreamEvent,
    PartDeltaEvent,
    PartStartEvent,
    TextPart,
    TextPartDelta,
    ToolCallPart,
)
from pydantic_ai.tools import DeferredToolRequests
from slack_sdk.web.async_chat_stream import AsyncChatStream
from slack_sdk.web.async_client import AsyncWebClient

from octomate.config import ChannelConfig, ChannelStreamConfig
from octomate.schemas.conversation import ConversationKey
from octomate.schemas.segments import AtSegment, ImageSegment, ReplySegment, TextSegment
from octomate.tentacles.channel.markdown import MarkdownChunker
from octomate.tentacles.channel.lark import LarkChromo, LarkInk, LarkTentacle
from octomate.tentacles.channel.lark.schema import LarkOutboundMessage, LarkStreamCard
from octomate.tentacles.channel.napcat import NapcatChromo, NapcatInk, NapcatTentacle
from octomate.tentacles.channel.napcat.schema import NapcatOutboundMessage
from octomate.tentacles.channel.slack import SlackChromo, SlackInk, SlackTentacle
from octomate.tentacles.channel.slack.ink import (
    SLACK_MARKDOWN_TEXT_LIMIT,
)
from octomate.tentacles.channel.slack.ink import SlackInk as SlackInkType
from octomate.tentacles.channel.slack.schema import SlackOutboundMessage

SlackEvent = AgentStreamEvent | AgentRunResultEvent[str]


def test_channel_thread_strategies_are_declared() -> None:
    assert SlackTentacle.thread_strategy == "flat_thread"
    assert LarkTentacle.thread_strategy == "flat_thread"
    assert NapcatTentacle.thread_strategy == "main_only"


async def test_napcat_chromo_decodes_group_message_segments() -> None:
    chromo = NapcatChromo()
    event = await chromo.sip(
        json.dumps(
            {
                "post_type": "message",
                "message_type": "group",
                "time": 1710000000,
                "self_id": 42,
                "message_id": 1001,
                "group_id": 2002,
                "user_id": 3003,
                "sender": {"user_id": 3003, "nickname": "Alice"},
                "raw_message": "hello",
                "message": [
                    {"type": "reply", "data": {"id": "999"}},
                    {"type": "text", "data": {"text": "hello "}},
                    {"type": "at", "data": {"qq": "42", "name": "Octomate"}},
                    {
                        "type": "image",
                        "data": {"file": "image-key", "url": "https://image"},
                    },
                    {"type": "face", "data": {"id": "14"}},
                ],
            }
        )
    )

    assert event is not None
    assert event.chat_type == "group"
    assert event.chat_id == "2002"
    assert event.user_id == "3003"
    assert event.self_id == "42"
    assert event.message_id == "1001"
    assert event.reply_id == "999"
    assert [type(seg) for seg in event.segments] == [
        ReplySegment,
        TextSegment,
        AtSegment,
        ImageSegment,
    ]
    at_seg = event.segments[2]
    image_seg = event.segments[3]
    assert isinstance(at_seg, AtSegment)
    assert isinstance(image_seg, ImageSegment)
    assert at_seg.data.user_id == "42"
    assert image_seg.data.file == "image-key"


async def test_napcat_chromo_decodes_private_message() -> None:
    chromo = NapcatChromo()
    event = await chromo.sip(
        json.dumps(
            {
                "post_type": "message",
                "message_type": "private",
                "time": 1710000000,
                "self_id": 42,
                "message_id": 1001,
                "user_id": 3003,
                "raw_message": "hello",
                "message": [{"type": "text", "data": {"text": "hello"}}],
            }
        )
    )

    assert event is not None
    assert event.chat_type == "private"
    assert event.chat_id == "3003"
    assert event.user_id == "3003"
    assert [type(seg) for seg in event.segments] == [TextSegment]


async def test_napcat_chromo_ignores_responses_and_non_message_events() -> None:
    chromo = NapcatChromo()

    response = await chromo.sip(json.dumps({"status": "ok", "retcode": 0}))
    notice = await chromo.sip(
        json.dumps(
            {
                "post_type": "notice",
                "notice_type": "group_recall",
                "message_id": 1,
            }
        )
    )

    assert response is None
    assert notice is None


async def test_napcat_chromo_renders_final_text_without_markdown() -> None:
    chromo = NapcatChromo()

    messages = chromo.squirt(AgentRunResult("hello **napcat**"))

    assert messages == [
        NapcatOutboundMessage(
            segments=[{"type": "text", "data": {"text": "hello napcat"}}]
        )
    ]


async def test_napcat_chromo_renders_structured_output_as_plain_json() -> None:
    class Answer(BaseModel):
        ok: bool
        count: int

    chromo = NapcatChromo()

    messages = chromo.squirt(AgentRunResult(Answer(ok=True, count=2)))
    text = messages[0].segments[0]["data"]["text"]

    assert text.startswith("{")
    assert '"ok": true' in text
    assert '"count": 2' in text


async def test_napcat_chromo_renders_deferred_requests_as_plain_text() -> None:
    chromo = NapcatChromo()

    messages = chromo.squirt(
        AgentRunResult(
            DeferredToolRequests(
                calls=[
                    ToolCallPart(
                        tool_name="ask_user",
                        args={"question": "Continue?"},
                        tool_call_id="call_1",
                    )
                ]
            )
        )
    )
    text = messages[0].segments[0]["data"]["text"]

    assert "ask_user needs input" in text
    assert "call_1" in text


class FakeNapcatResponse:
    def __init__(self, data: dict[str, object] | None = None) -> None:
        self._data = data or {"data": {"message_id": "msg-1"}}

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict[str, object]:
        return self._data


class FakeNapcatHTTP:
    def __init__(self) -> None:
        self.posts: list[tuple[str, dict[str, object]]] = []

    async def post(self, endpoint: str, json: dict[str, object]) -> FakeNapcatResponse:
        self.posts.append((endpoint, json))
        return FakeNapcatResponse()


async def test_napcat_ink_sends_group_private_and_reply_messages() -> None:
    http = FakeNapcatHTTP()
    ink = object.__new__(NapcatInk)
    cast(Any, ink).httpx = http
    message = NapcatOutboundMessage(
        segments=[{"type": "text", "data": {"text": "hello"}}]
    )

    group_id = await ink.send_message("2002", "group", [message], reply_to="1001")
    private_id = await ink.send_message("3003", "private", [message])

    assert group_id == "msg-1"
    assert private_id == "msg-1"
    assert http.posts == [
        (
            "/send_group_msg",
            {
                "group_id": "2002",
                "message": message.segments,
                "reply": "1001",
            },
        ),
        (
            "/send_private_msg",
            {"user_id": "3003", "message": message.segments},
        ),
    ]


async def test_napcat_tentacle_sense_invokes_ingest() -> None:
    channel = object.__new__(NapcatTentacle)
    calls: list[object] = []

    async def ingest(raw: object) -> None:
        calls.append(raw)

    class FakeWS:
        def __aiter__(self) -> AsyncIterator[str]:
            return self._events()

        async def _events(self) -> AsyncIterator[str]:
            yield "event-1"
            yield "event-2"

    cast(Any, channel).ingest = ingest

    await channel.sense(cast(Any, FakeWS()))

    assert calls == ["event-1", "event-2"]


async def test_napcat_tentacle_connects_with_auth_header(monkeypatch) -> None:
    calls: list[dict[str, object]] = []
    channel = object.__new__(NapcatTentacle)
    channel.id = "napcat"
    channel.ws_url = "ws://napcat"
    channel.access_token = SecretStr("token")
    channel.backoff_base = 0.01
    channel.backoff_max = 0.01
    channel.backoff_factor = 2.0
    channel.ws_client = None
    channel.stop_event = None

    class FakeConnect:
        def __init__(self, url: str, additional_headers: dict[str, str] | None) -> None:
            calls.append({"url": url, "additional_headers": additional_headers})

        async def __aenter__(self) -> object:
            return object()

        async def __aexit__(self, *args: object) -> None:
            return None

    async def sense(ws: object) -> None:
        assert channel.stop_event is not None
        channel.stop_event.set()

    channel.sense = sense
    monkeypatch.setattr(
        "octomate.tentacles.channel.napcat.base.connect",
        FakeConnect,
    )

    await channel.activate()

    assert calls == [
        {
            "url": "ws://napcat",
            "additional_headers": {"Authorization": "Bearer token"},
        }
    ]


def _lark_raw(
    *,
    message_type: str,
    chat_type: str,
    content: dict[str, Any],
    mentions: list[Any] | None = None,
    chat_id: str = "oc_group",
    sender_id: str = "ou_sender",
    message_id: str = "om_message",
    thread_id: str = "",
    parent_id: str = "",
) -> P2ImMessageReceiveV1:
    return cast(
        P2ImMessageReceiveV1,
        SimpleNamespace(
            event=SimpleNamespace(
                message=SimpleNamespace(
                    message_type=message_type,
                    chat_type=chat_type,
                    content=json.dumps(content),
                    mentions=mentions,
                    chat_id=chat_id,
                    message_id=message_id,
                    thread_id=thread_id,
                    parent_id=parent_id,
                ),
                sender=SimpleNamespace(sender_id=SimpleNamespace(open_id=sender_id)),
            )
        )
    )


async def test_lark_chromo_decodes_text_mentions_and_reply_metadata() -> None:
    chromo = LarkChromo()
    raw = _lark_raw(
        message_type="text",
        chat_type="group",
        content={"text": "hello @user world"},
        mentions=[
            SimpleNamespace(
                key="@user",
                id=SimpleNamespace(open_id="ou_mentioned"),
                name="Mentioned User",
            )
        ],
        thread_id="omt_thread",
        parent_id="om_parent",
    )

    event = await chromo.sip(raw)

    assert event is not None
    assert event.chat_type == "group"
    assert event.chat_id == "oc_group"
    assert event.user_id == "ou_sender"
    assert event.message_id == "om_message"
    assert event.thread_id == "omt_thread"
    assert event.reply_id == "om_parent"
    assert [type(seg) for seg in event.segments] == [
        ReplySegment,
        TextSegment,
        AtSegment,
        TextSegment,
    ]
    mention_seg = event.segments[2]
    assert isinstance(mention_seg, AtSegment)
    assert mention_seg.data.user_id == "ou_mentioned"


async def test_lark_chromo_decodes_private_images() -> None:
    chromo = LarkChromo()

    event = await chromo.sip(
        _lark_raw(
            message_type="image",
            chat_type="p2p",
            content={"image_key": "img_key"},
            sender_id="ou_private",
        )
    )

    assert event is not None
    assert event.chat_type == "private"
    assert event.chat_id == "ou_private"
    assert [type(seg) for seg in event.segments] == [ImageSegment]
    image_seg = event.segments[0]
    assert isinstance(image_seg, ImageSegment)
    assert image_seg.data.file == "img_key"


async def test_lark_chromo_decodes_post_content() -> None:
    chromo = LarkChromo()

    event = await chromo.sip(
        _lark_raw(
            message_type="post",
            chat_type="group",
            content={
                "title": "Release",
                "zh_cn": [
                    [
                        {"tag": "text", "text": "ready "},
                        {"tag": "a", "href": "https://example.com"},
                        {
                            "tag": "at",
                            "user_id": "ou_reviewer",
                            "user_name": "Reviewer",
                        },
                        {"tag": "img", "image_key": "img_post"},
                    ]
                ],
            },
        )
    )

    assert event is not None
    assert [type(seg) for seg in event.segments] == [
        TextSegment,
        TextSegment,
        TextSegment,
        AtSegment,
        ImageSegment,
    ]
    assert event.text_parts() == ["[Release]\n", "ready ", "https://example.com"]
    at_seg = event.segments[3]
    image_seg = event.segments[4]
    assert isinstance(at_seg, AtSegment)
    assert isinstance(image_seg, ImageSegment)
    assert at_seg.data.user_id == "ou_reviewer"
    assert image_seg.data.file == "img_post"


async def test_lark_chromo_renders_final_text_as_interactive_markdown() -> None:
    chromo = LarkChromo()

    messages = chromo.squirt(AgentRunResult("hello **lark**"))

    assert len(messages) == 1
    assert messages[0].msg_type == "interactive"
    content = json.loads(messages[0].content)
    assert content["body"]["elements"] == [
        {"tag": "markdown", "content": "hello **lark**"}
    ]


async def test_lark_chromo_builds_streaming_card_payload() -> None:
    chromo = LarkChromo()

    data = json.loads(chromo.make_stream_card_data("hello"))
    message = chromo.make_stream_card_message("card-1")

    assert data["schema"] == "2.0"
    assert data["config"]["streaming_mode"] is True
    assert data["config"]["streaming_config"]["print_frequency_ms"]["default"] == 20
    assert data["config"]["streaming_config"]["print_step"]["default"] == 12
    assert data["body"]["elements"] == [
        {
            "tag": "markdown",
            "content": "hello",
            "element_id": "octomate_answer",
        }
    ]
    assert json.loads(message.content) == {
        "type": "card",
        "data": {"card_id": "card-1"},
    }


async def test_lark_chromo_renders_structured_output_as_json_card() -> None:
    class Answer(BaseModel):
        ok: bool
        count: int

    chromo = LarkChromo()

    messages = chromo.squirt(AgentRunResult(Answer(ok=True, count=2)))
    content = json.loads(messages[0].content)
    markdown = content["body"]["elements"][0]["content"]

    assert markdown.startswith("```json")
    assert '"ok": true' in markdown
    assert '"count": 2' in markdown


async def test_lark_chromo_renders_deferred_requests_as_markdown_card() -> None:
    chromo = LarkChromo()

    messages = chromo.squirt(
        AgentRunResult(
            DeferredToolRequests(
                calls=[
                    ToolCallPart(
                        tool_name="ask_user",
                        args={"question": "Continue?"},
                        tool_call_id="call_1",
                    )
                ]
            )
        )
    )
    content = json.loads(messages[0].content)
    markdown = content["body"]["elements"][0]["content"]

    assert "`ask_user` needs input" in markdown
    assert "`call_1`" in markdown


class FakeLarkInk(LarkInk):
    def __init__(self) -> None:
        self.created: list[tuple[str, str, str, str]] = []
        self.replies: list[tuple[str, str, str, bool]] = []
        self.stream_cards: list[tuple[str, str]] = []
        self.stream_messages: list[
            tuple[str, str, LarkStreamCard, str | None, bool]
        ] = []
        self.stream_updates: list[tuple[LarkStreamCard, str, int]] = []
        self.fail_stream_create = False
        self.fail_stream_update = False

    async def _create_message(
        self,
        receive_id: str,
        receive_id_type: str,
        msg_type: str,
        content: str,
    ) -> str:
        self.created.append((receive_id, receive_id_type, msg_type, content))
        return f"created-{len(self.created)}"

    async def _reply_message(
        self,
        message_id: str,
        msg_type: str,
        content: str,
        *,
        reply_in_thread: bool = False,
    ) -> str:
        self.replies.append((message_id, msg_type, content, reply_in_thread))
        return f"reply-{len(self.replies)}"

    async def create_stream_card(
        self,
        card_data: str,
        *,
        element_id: str,
    ) -> LarkStreamCard | None:
        self.stream_cards.append((card_data, element_id))
        if self.fail_stream_create:
            return None
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


async def test_lark_ink_selects_group_and_private_targets() -> None:
    ink = FakeLarkInk()
    message = LarkOutboundMessage(msg_type="interactive", content="{}")

    group_id = await ink.send_message("oc_group", "group", [message])
    private_id = await ink.send_message("ou_user", "private", [message])

    assert group_id == "created-1"
    assert private_id == "created-2"
    assert ink.created == [
        ("oc_group", "chat_id", "interactive", "{}"),
        ("ou_user", "open_id", "interactive", "{}"),
    ]


async def test_lark_ink_replies_to_first_message_unless_threaded() -> None:
    ink = FakeLarkInk()
    messages = [
        LarkOutboundMessage(msg_type="interactive", content="one"),
        LarkOutboundMessage(msg_type="interactive", content="two"),
    ]

    first_id = await ink.send_message("oc_group", "group", messages, "om_parent")

    assert first_id == "reply-1"
    assert ink.replies == [("om_parent", "interactive", "one", False)]
    assert ink.created == [("oc_group", "chat_id", "interactive", "two")]


async def test_lark_ink_replies_to_each_message_when_threaded() -> None:
    ink = FakeLarkInk()
    messages = [
        LarkOutboundMessage(msg_type="interactive", content="one"),
        LarkOutboundMessage(msg_type="interactive", content="two"),
    ]

    first_id = await ink.send_message(
        "oc_group",
        "group",
        messages,
        "om_parent",
        reply_in_thread=True,
    )

    assert first_id == "reply-1"
    assert ink.created == []
    assert ink.replies == [
        ("om_parent", "interactive", "one", True),
        ("om_parent", "interactive", "two", True),
    ]


def _lark_channel(ink: FakeLarkInk) -> LarkTentacle:
    channel = object.__new__(LarkTentacle)
    channel.id = "lark"
    channel.ink = ink
    channel.chromo = LarkChromo()
    channel.config = ChannelConfig(
        type="lark",
        stream=ChannelStreamConfig(flush_interval=0.2, min_chars=1),
    )
    return channel


def _enable_lark_stream(channel: LarkTentacle, *, interval: float = 0.2) -> None:
    channel.config.stream = ChannelStreamConfig(flush_interval=interval, min_chars=1)


async def test_lark_tentacle_replies_to_key_thread_id_when_it_is_open_message_id() -> None:
    ink = FakeLarkInk()
    channel = _lark_channel(ink)
    key = ConversationKey(
        channel_tentacle_id="lark",
        chat_type="group",
        chat_id="oc_group",
        user_id="ou_user",
        thread_id="om_child_message",
    )

    await channel.respond(key, AgentRunResult("thread reply"))

    assert ink.created == []
    assert len(ink.replies) == 1
    message_id, msg_type, content, reply_in_thread = ink.replies[0]
    assert message_id == "om_child_message"
    assert msg_type == "interactive"
    assert "thread reply" in content
    assert reply_in_thread is True


async def test_lark_tentacle_private_thread_uses_reply_in_thread() -> None:
    ink = FakeLarkInk()
    channel = _lark_channel(ink)
    key = ConversationKey(
        channel_tentacle_id="lark",
        chat_type="private",
        chat_id="ou_user",
        user_id="ou_user",
        thread_id="om_private_anchor",
    )

    await channel.respond(key, AgentRunResult("private thread reply"))

    assert ink.created == []
    assert len(ink.replies) == 1
    message_id, msg_type, content, reply_in_thread = ink.replies[0]
    assert message_id == "om_private_anchor"
    assert msg_type == "interactive"
    assert "private thread reply" in content
    assert reply_in_thread is True


async def test_lark_tentacle_ignores_thread_id_as_reply_target() -> None:
    ink = FakeLarkInk()
    channel = _lark_channel(ink)
    key = ConversationKey(
        channel_tentacle_id="lark",
        chat_type="group",
        chat_id="oc_group",
        user_id="ou_user",
        thread_id="omt_19766a1bf00edb8e",
    )

    await channel.respond(key, AgentRunResult("new message"))

    assert ink.replies == []
    assert ink.created[0][:2] == ("oc_group", "chat_id")


async def test_lark_tentacle_streams_batched_card_updates_in_reply_thread() -> None:
    ink = FakeLarkInk()
    channel = _lark_channel(ink)
    _enable_lark_stream(channel, interval=0.2)
    key = ConversationKey(
        channel_tentacle_id="lark",
        chat_type="group",
        chat_id="oc_group",
        user_id="ou_user",
        thread_id="om_parent",
    )
    drained: list[str] = []

    async def events() -> AsyncIterator[AgentStreamEvent | AgentRunResultEvent[str]]:
        yield PartStartEvent(index=0, part=TextPart(content="he"))
        yield PartDeltaEvent(index=0, delta=TextPartDelta(content_delta="llo"))
        yield AgentRunResultEvent(AgentRunResult("hello"))
        assert ink.stream_updates == []
        drained.append("after-final")
        yield PartDeltaEvent(index=0, delta=TextPartDelta(content_delta=" ignored"))

    await channel.stream_respond(key, events())

    assert len(ink.stream_cards) == 1
    assert json.loads(ink.stream_cards[0][0])["config"]["streaming_mode"] is True
    assert ink.stream_messages == [
        (
            "oc_group",
            "group",
            LarkStreamCard(card_id="card-1", element_id="octomate_answer"),
            "om_parent",
            True,
        )
    ]
    assert ink.stream_updates == [
        (
            LarkStreamCard(card_id="card-1", element_id="octomate_answer"),
            "hello",
            1,
        )
    ]
    assert drained == ["after-final"]
    assert ink.created == []
    assert ink.replies == []


async def test_lark_tentacle_can_stream_immediate_updates_when_configured() -> None:
    ink = FakeLarkInk()
    channel = _lark_channel(ink)
    _enable_lark_stream(channel, interval=0)
    key = ConversationKey(
        channel_tentacle_id="lark",
        chat_type="private",
        chat_id="ou_user",
        user_id="ou_user",
    )

    async def events() -> AsyncIterator[AgentStreamEvent | AgentRunResultEvent[str]]:
        yield PartStartEvent(index=0, part=TextPart(content="he"))
        yield PartDeltaEvent(index=0, delta=TextPartDelta(content_delta="llo"))
        yield AgentRunResultEvent(AgentRunResult("hello"))

    await channel.stream_respond(key, events())

    assert [(content, sequence) for _, content, sequence in ink.stream_updates] == [
        ("he", 1),
        ("hello", 2),
    ]


async def test_lark_tentacle_falls_back_to_final_message_on_stream_failure() -> None:
    ink = FakeLarkInk()
    ink.fail_stream_create = True
    channel = _lark_channel(ink)
    _enable_lark_stream(channel, interval=0)
    key = ConversationKey(
        channel_tentacle_id="lark",
        chat_type="private",
        chat_id="ou_user",
        user_id="ou_user",
    )

    async def events() -> AsyncIterator[AgentStreamEvent | AgentRunResultEvent[str]]:
        yield PartStartEvent(index=0, part=TextPart(content="hello"))
        yield AgentRunResultEvent(AgentRunResult("hello"))

    await channel.stream_respond(key, events())

    assert ink.stream_messages == []
    assert ink.created[0][:3] == ("ou_user", "open_id", "interactive")
    assert "hello" in ink.created[0][3]


async def test_lark_tentacle_stops_stream_on_deferred_result() -> None:
    ink = FakeLarkInk()
    channel = _lark_channel(ink)
    _enable_lark_stream(channel, interval=0)
    key = ConversationKey(
        channel_tentacle_id="lark",
        chat_type="private",
        chat_id="ou_user",
        user_id="ou_user",
    )
    drained: list[str] = []

    async def events() -> AsyncIterator[
        AgentStreamEvent | AgentRunResultEvent[DeferredToolRequests]
    ]:
        yield PartStartEvent(index=0, part=TextPart(content="partial"))
        yield AgentRunResultEvent(
            AgentRunResult(
                DeferredToolRequests(
                    calls=[
                        ToolCallPart(
                            tool_name="ask_user",
                            args={"question": "Continue?"},
                            tool_call_id="call_1",
                        )
                    ]
                )
            )
        )
        drained.append("after-deferred")
        yield PartDeltaEvent(index=0, delta=TextPartDelta(content_delta=" ignored"))

    await channel.stream_respond(key, events())

    assert [(content, sequence) for _, content, sequence in ink.stream_updates] == [
        ("partial", 1)
    ]
    assert drained == ["after-deferred"]
    assert ink.created == []
    assert ink.replies == []


async def test_lark_tentacle_message_callback_invokes_ingest() -> None:
    channel = object.__new__(LarkTentacle)
    raw = object()
    calls: list[object] = []
    done = asyncio.Event()

    async def ingest(raw: object) -> None:
        calls.append(raw)
        done.set()

    channel.ingest = ingest

    channel.sense(cast(Any, raw))
    await asyncio.wait_for(done.wait(), timeout=1)

    assert calls == [raw]


async def test_slack_chromo_decodes_mentions_and_images() -> None:
    chromo = SlackChromo()
    event = await chromo.sip(
        {
            "ts": "1710000000.000100",
            "user": "U1",
            "channel": "C1",
            "channel_type": "channel",
            "text": "hi <@U2>",
            "files": [
                {
                    "mimetype": "image/png",
                    "url_private": "https://files/image.png",
                    "name": "image.png",
                }
            ],
        }
    )

    assert event is not None
    assert event.chat_type == "group"
    assert [type(seg) for seg in event.segments] == [
        TextSegment,
        AtSegment,
        ImageSegment,
    ]


async def test_slack_chromo_renders_final_text_result() -> None:
    chromo = SlackChromo()
    markdown = (
        "# Hello Slack\n\n"
        "Keep **bold**, [links](https://example.com), and tables intact.\n\n"
        "| a | b |\n| - | - |\n| 1 | 2 |"
    )

    messages = chromo.squirt(AgentRunResult(markdown))

    assert len(messages) == 1
    assert messages[0].text == markdown
    assert messages[0].markdown_text == markdown
    assert messages[0].blocks is None


async def test_slack_chromo_renders_structured_output_as_json() -> None:
    class Answer(BaseModel):
        ok: bool
        count: int

    chromo = SlackChromo()

    messages = chromo.squirt(AgentRunResult(Answer(ok=True, count=2)))

    assert len(messages) == 1
    assert messages[0].text.startswith("```json")
    assert messages[0].markdown_text == messages[0].text
    assert '"ok": true' in messages[0].text
    assert '"count": 2' in messages[0].text


async def test_slack_chromo_renders_deferred_requests_as_markdown() -> None:
    chromo = SlackChromo()

    messages = chromo.squirt(
        AgentRunResult(
            DeferredToolRequests(
                calls=[
                    ToolCallPart(
                        tool_name="ask_user",
                        args={"question": "Continue?"},
                        tool_call_id="call_1",
                    )
                ]
            )
        )
    )

    assert len(messages) == 1
    assert messages[0].markdown_text is not None
    assert "`ask_user` needs input" in messages[0].markdown_text
    assert "`call_1`" in messages[0].markdown_text


@dataclass
class FakeSlackInk:
    streams: list[dict[str, str | None]] = field(default_factory=list)
    appends: list[str] = field(default_factory=list)
    stops: list[str | None] = field(default_factory=list)
    finals: list[dict[str, str | None]] = field(default_factory=list)
    sent: list[tuple[str, str, list[SlackOutboundMessage], str | None]] = field(
        default_factory=list
    )

    async def start_stream(
        self,
        channel: str,
        thread_ts: str,
        *,
        recipient_user_id: str | None = None,
        recipient_team_id: str | None = None,
    ) -> object:
        self.streams.append(
            {
                "channel": channel,
                "thread_ts": thread_ts,
                "recipient_user_id": recipient_user_id,
                "recipient_team_id": recipient_team_id,
            }
        )
        return object()

    @asynccontextmanager
    async def open_stream(
        self,
        channel: str,
        thread_ts: str,
        *,
        recipient_user_id: str | None = None,
        recipient_team_id: str | None = None,
    ) -> AsyncIterator[object]:
        stream = await self.start_stream(
            channel,
            thread_ts,
            recipient_user_id=recipient_user_id,
            recipient_team_id=recipient_team_id,
        )
        try:
            yield stream
        finally:
            await self.stop_stream(stream)

    async def append_stream(self, stream: object, markdown_text: str) -> None:
        self.appends.append(markdown_text)

    async def stop_stream(
        self,
        stream: object,
        *,
        markdown_text: str | None = None,
    ) -> str:
        self.stops.append(markdown_text)
        return "stream-ts"

    async def stream_markdown(
        self,
        channel: str,
        thread_ts: str,
        markdown_text: str,
        *,
        recipient_user_id: str | None = None,
        recipient_team_id: str | None = None,
    ) -> str:
        self.finals.append(
            {
                "channel": channel,
                "thread_ts": thread_ts,
                "markdown_text": markdown_text,
                "recipient_user_id": recipient_user_id,
                "recipient_team_id": recipient_team_id,
            }
        )
        return "stream-ts"

    async def send_message(
        self,
        chat_id: str,
        chat_type: str,
        messages: list[SlackOutboundMessage],
        reply_to: str | None = None,
    ) -> str:
        self.sent.append((chat_id, chat_type, messages, reply_to))
        return "fallback-ts"


def _slack_channel(ink: FakeSlackInk) -> SlackTentacle:
    channel = object.__new__(SlackTentacle)
    channel.id = "slack"
    channel.ink = cast(SlackInkType, ink)
    channel.chromo = SlackChromo()
    channel.config = ChannelConfig(
        type="slack",
        stream=ChannelStreamConfig(flush_interval=0),
    )
    return channel


def _slack_key() -> ConversationKey:
    return ConversationKey(
        channel_tentacle_id="slack",
        chat_type="group",
        chat_id="C1",
        user_id="U1",
        thread_id="1710000000.000100",
    )


async def test_slack_tentacle_streams_text_deltas_in_source_thread() -> None:
    ink = FakeSlackInk()
    channel = _slack_channel(ink)
    drained: list[str] = []

    async def events() -> AsyncIterator[SlackEvent]:
        yield PartStartEvent(index=0, part=TextPart(content="# Hello\n"))
        yield PartDeltaEvent(index=0, delta=TextPartDelta(content_delta="**world**"))
        yield AgentRunResultEvent(AgentRunResult("# Hello\n**world**"))
        drained.append("after-final")
        yield PartDeltaEvent(index=0, delta=TextPartDelta(content_delta=" ignored"))

    await channel.stream_respond(_slack_key(), events())

    assert ink.streams == [
        {
            "channel": "C1",
            "thread_ts": "1710000000.000100",
            "recipient_user_id": "U1",
            "recipient_team_id": None,
        }
    ]
    assert ink.appends == ["# Hello\n", "**world**"]
    assert ink.stops == [None]
    assert ink.finals == []
    assert drained == ["after-final"]


async def test_slack_tentacle_can_batch_stream_deltas_when_configured() -> None:
    ink = FakeSlackInk()
    channel = _slack_channel(ink)
    channel.config.stream = ChannelStreamConfig(flush_interval=999, min_chars=100)
    drained: list[str] = []

    async def events() -> AsyncIterator[SlackEvent]:
        yield PartStartEvent(index=0, part=TextPart(content="# Hello\n"))
        yield PartDeltaEvent(index=0, delta=TextPartDelta(content_delta="**world**"))
        yield AgentRunResultEvent(AgentRunResult("# Hello\n**world**"))
        assert ink.appends == []
        drained.append("after-final")

    await channel.stream_respond(_slack_key(), events())

    assert drained == ["after-final"]
    assert ink.appends == ["# Hello\n**world**"]
    assert ink.stops == [None]


async def test_slack_tentacle_stops_stream_on_deferred_result() -> None:
    ink = FakeSlackInk()
    channel = _slack_channel(ink)
    drained: list[str] = []

    async def events() -> AsyncIterator[
        AgentStreamEvent | AgentRunResultEvent[DeferredToolRequests]
    ]:
        yield PartStartEvent(index=0, part=TextPart(content="partial"))
        yield AgentRunResultEvent(
            AgentRunResult(
                DeferredToolRequests(
                    calls=[
                        ToolCallPart(
                            tool_name="ask_user",
                            args={"question": "Continue?"},
                            tool_call_id="call_1",
                        )
                    ]
                )
            )
        )
        drained.append("after-deferred")
        yield PartDeltaEvent(index=0, delta=TextPartDelta(content_delta=" ignored"))

    await channel.stream_respond(_slack_key(), events())

    assert ink.appends == ["partial"]
    assert ink.sent == []
    assert drained == ["after-deferred"]


async def test_slack_tentacle_streams_final_only_result_once() -> None:
    ink = FakeSlackInk()
    channel = _slack_channel(ink)

    async def events() -> AsyncIterator[SlackEvent]:
        yield AgentRunResultEvent(AgentRunResult("final **markdown**"))

    await channel.stream_respond(_slack_key(), events())

    assert len(ink.streams) == 1
    assert ink.appends == ["final **markdown**"]
    assert ink.stops == [None]
    assert ink.finals == []


async def test_slack_tentacle_closes_open_stream_on_append_error() -> None:
    class RaisingSlackInk(FakeSlackInk):
        async def append_stream(self, stream: object, markdown_text: str) -> None:
            await super().append_stream(stream, markdown_text)
            raise RuntimeError("append failed")

    ink = RaisingSlackInk()
    channel = _slack_channel(ink)

    async def events() -> AsyncIterator[SlackEvent]:
        yield PartStartEvent(index=0, part=TextPart(content="hello"))

    await channel.stream_respond(_slack_key(), events())

    assert ink.appends == ["hello"]
    assert ink.stops == [None]


class FakeSlackClient:
    def __init__(self) -> None:
        self.streams: list[dict[str, object]] = []
        self.uploads: list[dict[str, object]] = []

    async def chat_stream(self, **kwargs: object) -> object:
        self.streams.append(kwargs)
        return object()

    async def files_upload_v2(self, **kwargs: object) -> dict[str, object]:
        self.uploads.append(kwargs)
        return {"file": {"permalink": "https://slack/files/1"}}


async def test_slack_ink_uploads_long_markdown_instead_of_truncating() -> None:
    client = FakeSlackClient()
    ink = object.__new__(SlackInk)
    ink.client = cast(AsyncWebClient, client)

    content = "x" * (SLACK_MARKDOWN_TEXT_LIMIT + 1)
    result = await ink.stream_markdown("C1", "1710000000.000100", content)

    assert result == "https://slack/files/1"
    assert client.streams == []
    assert client.uploads[0]["channel"] == "C1"
    assert client.uploads[0]["thread_ts"] == "1710000000.000100"
    assert client.uploads[0]["content"] == content


async def test_slack_ink_flushes_each_stream_append() -> None:
    class FakeSlackStream:
        def __init__(self) -> None:
            self.appends: list[dict[str, object]] = []

        async def append(self, **kwargs: object) -> None:
            self.appends.append(kwargs)

    ink = object.__new__(SlackInk)
    stream = FakeSlackStream()

    await ink.append_stream(cast(AsyncChatStream, stream), "hello")

    assert stream.appends == [{"markdown_text": "hello", "chunks": ()}]


async def test_slack_tentacle_ensures_assistant_thread_conversation() -> None:
    class FakeConversations:
        def __init__(self) -> None:
            self.calls: list[tuple[ConversationKey, str | None]] = []

        async def ensure(
            self,
            key: ConversationKey,
            *,
            agent_tentacle_id: str | None = None,
        ) -> object:
            self.calls.append((key, agent_tentacle_id))
            return object()

    conversations = FakeConversations()
    channel = _slack_channel(FakeSlackInk())
    cast(Any, channel).octomate = SimpleNamespace(conversations=conversations)
    channel.agent_id = "inkling"

    await channel.on_assistant_thread_started(
        {
            "type": "assistant_thread_started",
            "assistant_thread": {
                "user_id": "U1",
                "channel_id": "D1",
                "thread_ts": "1710000000.000100",
                "context": {"channel_id": "C1", "team_id": "T1"},
            },
            "event_ts": "1710000000.000200",
        }
    )

    assert len(conversations.calls) == 1
    key, agent_id = conversations.calls[0]
    assert key == ConversationKey(
        channel_tentacle_id="slack",
        chat_type="private",
        chat_id="D1",
        user_id="U1",
        thread_id="1710000000.000100",
    )
    assert agent_id == "inkling"


def test_markdown_chunker_prefers_paragraph_boundaries() -> None:
    chunker = MarkdownChunker()
    first = ("a" * (SLACK_MARKDOWN_TEXT_LIMIT // 2)) + "\n\n"
    second = "b" * (SLACK_MARKDOWN_TEXT_LIMIT // 2)
    text = first + second

    chunks = chunker.chunk(text)

    assert "".join(chunks) == text
    assert all(len(chunk) <= SLACK_MARKDOWN_TEXT_LIMIT for chunk in chunks)
    assert chunks[0] == first


def test_markdown_chunker_prefers_word_boundaries_before_hard_cutting() -> None:
    chunker = MarkdownChunker()
    text = "word " * ((SLACK_MARKDOWN_TEXT_LIMIT // 5) + 10)

    chunks = chunker.chunk(text)

    assert "".join(chunks) == text
    assert all(len(chunk) <= SLACK_MARKDOWN_TEXT_LIMIT for chunk in chunks)
    assert chunks[0].endswith(" ")
