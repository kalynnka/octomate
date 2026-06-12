from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from uuid import uuid4
from types import SimpleNamespace, TracebackType
from typing import Generic, TypeVar, cast

import httpx
from lark_oapi.api.im.v1.model.p2_im_message_receive_v1 import P2ImMessageReceiveV1
from pydantic import SecretStr, TypeAdapter
from pydantic_ai import AgentRunResult, AgentRunResultEvent
from pydantic_ai.messages import (
    FunctionToolCallEvent,
    FunctionToolResultEvent,
    PartStartEvent,
    TextPart,
    ThinkingPart,
    ToolCallPart,
    ToolReturnPart,
)
from pydantic_ai.result import FinalResult, StreamedRunResult
from pydantic_ai.tools import DeferredToolRequests
from slack_sdk.models.messages.chunk import Chunk, TaskUpdateChunk
from slack_sdk.web.async_chat_stream import AsyncChatStream
from slack_sdk.web.async_client import AsyncWebClient
from websockets.asyncio.client import ClientConnection

from octomate import Octomate
from octomate.capabilities.events import (
    ResultSegmentEvent,
    ResultTextDeltaEvent,
    StreamEvents,
    TodoCompletedEvent,
    TodoCreatedEvent,
    TodoStatusChangedEvent,
)
from octomate.config import (
    LarkChannelConfig,
    LarkStreamConfig,
    SlackChannelConfig,
    SlackStreamConfig,
)
from octomate.schemas.conversation import Conversation, ConversationKey, UserProfile
from octomate.schemas.segments import (
    AtSegment,
    CardData,
    CardSegment,
    ImageData,
    ImageSegment,
    MarkdownSegment,
    ReplySegment,
    TextSegment,
)
from octomate.schemas.todos import Todo
from octomate.tentacles.channel.base import ChannelOutput, DownloadedImage, Ink
from octomate.tentacles.channel.feelers.base import Feelers
from octomate.tentacles.channel.feelers.output import (
    DefaultMarkdownFeeler,
    MarkdownChunker,
)
from octomate.tentacles.channel.lark import LarkChromo, LarkInk, LarkTentacle
from octomate.tentacles.channel.lark.feelers.approvals import LarkApprovalFeeler
from octomate.tentacles.channel.lark.feelers.output import (
    LarkMarkdownFeeler,
    LarkMarkdownStreamFeeler,
    LarkTimelineFeeler,
)
from octomate.tentacles.channel.lark.feelers.questions import LarkAskQuestionFeeler
from octomate.tentacles.channel.lark.schema import LarkOutboundMessage, LarkStreamCard
from octomate.tentacles.channel.napcat import NapcatChromo, NapcatInk, NapcatTentacle
from octomate.tentacles.channel.napcat.schema import NapcatOutboundMessage
from octomate.tentacles.channel.slack import SlackChromo, SlackInk, SlackTentacle
from octomate.tentacles.channel.slack.feelers.approvals import SlackApprovalFeeler
from octomate.tentacles.channel.slack.feelers.output import (
    SlackMarkdownStreamFeeler,
    SlackTimelineFeeler,
)
from octomate.tentacles.channel.slack.feelers.questions import SlackAskQuestionFeeler
from octomate.tentacles.channel.slack.ink import (
    SLACK_MARKDOWN_TEXT_LIMIT,
)
from octomate.tentacles.channel.slack.ink import SlackInk as SlackInkType
from octomate.tentacles.channel.slack.schema import SlackOutboundMessage
from octomate.types.json import JsonObject

StreamOutputT = TypeVar("StreamOutputT", bound=str | DeferredToolRequests | None)
JsonObjectAdapter = TypeAdapter(JsonObject)


@dataclass
class FakeStreamedRunResult(Generic[StreamOutputT]):
    output: StreamOutputT
    text_deltas: list[str] = field(default_factory=list)
    fail_text_stream: bool = False

    async def stream_text(
        self,
        *,
        delta: bool = False,
        debounce_by: float | None = 0.1,
    ) -> AsyncIterator[str]:
        if self.fail_text_stream or not isinstance(self.output, str):
            raise RuntimeError("text stream unavailable")
        for text in self.text_deltas or [self.output]:
            yield text

    async def stream_output(
        self,
        *,
        debounce_by: float | None = 0.1,
    ) -> AsyncIterator[StreamOutputT]:
        if self.fail_text_stream:
            raise RuntimeError("output stream unavailable")
        if isinstance(self.output, str):
            content = ""
            for text in self.text_deltas or [self.output]:
                content += text
                yield cast(StreamOutputT, content)
            return
        yield self.output

    async def get_output(self) -> StreamOutputT:
        return self.output


def streamed_result(
    output: StreamOutputT,
    *text_deltas: str,
    fail_text_stream: bool = False,
) -> StreamedRunResult[None, StreamOutputT]:
    return cast(
        StreamedRunResult[None, StreamOutputT],
        FakeStreamedRunResult(
            output=output,
            text_deltas=list(text_deltas),
            fail_text_stream=fail_text_stream,
        ),
    )


def _loaded_json_object(value: str) -> JsonObject:
    return JsonObjectAdapter.validate_json(value)


async def _output_events(
    *deltas: str,
) -> AsyncIterator[ResultTextDeltaEvent | FinalResult[ChannelOutput]]:
    """A streamed text reply as the typed output event stream a feeler's
    `present_output` consumes: each chunk as an additive `ResultTextDeltaEvent` (Pydantic
    AI's typewriter), then the joined whole as `FinalResult`."""
    for delta in deltas:
        yield ResultTextDeltaEvent(delta=delta)
    if deltas:
        yield FinalResult[ChannelOutput](output="".join(deltas))


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


def test_napcat_chromo_renders_markdown_as_plain_text() -> None:
    chromo = NapcatChromo()

    messages = chromo.outbound_markdown("hello **napcat**")

    assert messages == [
        NapcatOutboundMessage(
            segments=[{"type": "text", "data": {"text": "hello napcat"}}]
        )
    ]


class FakeNapcatResponse:
    def __init__(self, data: JsonObject | None = None) -> None:
        self._data = data or {"data": {"message_id": "msg-1"}}

    def raise_for_status(self) -> None:
        return None

    def json(self) -> JsonObject:
        return self._data


class FakeNapcatHTTP:
    def __init__(self) -> None:
        self.posts: list[tuple[str, JsonObject]] = []

    async def post(self, endpoint: str, json: JsonObject) -> FakeNapcatResponse:
        self.posts.append((endpoint, json))
        return FakeNapcatResponse()


async def test_napcat_ink_sends_group_private_and_reply_messages() -> None:
    http = FakeNapcatHTTP()
    ink = object.__new__(NapcatInk)
    ink.httpx = cast(httpx.AsyncClient, http)
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
    calls: list[str | bytes] = []

    async def ingest(raw: str | bytes) -> None:
        calls.append(raw)

    class FakeWS:
        def __aiter__(self) -> AsyncIterator[str]:
            return self._events()

        async def _events(self) -> AsyncIterator[str]:
            yield "event-1"
            yield "event-2"

    channel.ingest = ingest

    await channel.sense(cast(ClientConnection, FakeWS()))

    assert calls == ["event-1", "event-2"]


async def test_napcat_tentacle_connects_with_auth_header(monkeypatch) -> None:
    calls: list[tuple[str, dict[str, str] | None]] = []
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
            calls.append((url, additional_headers))

        async def __aenter__(self) -> ClientConnection:
            return cast(ClientConnection, SimpleNamespace())

        async def __aexit__(
            self,
            exc_type: type[BaseException] | None,
            exc: BaseException | None,
            traceback: TracebackType | None,
        ) -> None:
            return None

    async def sense(ws: ClientConnection) -> None:
        assert channel.stop_event is not None
        channel.stop_event.set()

    channel.sense = sense
    monkeypatch.setattr(
        "octomate.tentacles.channel.napcat.base.connect",
        FakeConnect,
    )

    await channel.activate()

    assert calls == [
        ("ws://napcat", {"Authorization": "Bearer token"}),
    ]


def _lark_raw(
    *,
    message_type: str,
    chat_type: str,
    content: JsonObject,
    mentions: list[JsonObject] | None = None,
    chat_id: str = "oc_group",
    sender_id: str = "ou_sender",
    message_id: str = "om_message",
    thread_id: str = "",
    parent_id: str = "",
    root_id: str = "",
) -> P2ImMessageReceiveV1:
    return P2ImMessageReceiveV1(
        {
            "event": {
                "message": {
                    "message_type": message_type,
                    "chat_type": chat_type,
                    "content": json.dumps(content),
                    "mentions": mentions,
                    "chat_id": chat_id,
                    "message_id": message_id,
                    "thread_id": thread_id,
                    "parent_id": parent_id,
                    "root_id": root_id,
                },
                "sender": {"sender_id": {"open_id": sender_id}},
            }
        }
    )


async def test_lark_chromo_decodes_text_mentions_and_reply_metadata() -> None:
    chromo = LarkChromo()
    raw = _lark_raw(
        message_type="text",
        chat_type="group",
        content={"text": "hello @user world"},
        mentions=[
            {
                "key": "@user",
                "id": {"open_id": "ou_mentioned"},
                "name": "Mentioned User",
            }
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


async def test_lark_chromo_keys_threaded_reply_on_root_message() -> None:
    # A reply inside a sub-thread carries the thread id ("omt_…") but must key
    # on the thread's root message ("om_…") so it maps to the sub-thread
    # conversation start_sub_thread created and the feeler can reply back in.
    chromo = LarkChromo()
    event = await chromo.sip(
        _lark_raw(
            message_type="text",
            chat_type="group",
            content={"text": "write another poem"},
            thread_id="omt_1945915eed8e1b85",
            root_id="om_x100b6d35d7b134b0c29324d97adc020",
            parent_id="om_some_reply",
        )
    )

    assert event is not None
    assert event.thread_id == "om_x100b6d35d7b134b0c29324d97adc020"
    assert event.reply_id == "om_some_reply"


async def test_lark_chromo_ignores_thread_without_thread_id() -> None:
    # A plain reply (root_id set, no Lark thread) is not a sub-thread.
    chromo = LarkChromo()
    event = await chromo.sip(
        _lark_raw(
            message_type="text",
            chat_type="group",
            content={"text": "hi"},
            root_id="om_root",
            parent_id="om_root",
        )
    )

    assert event is not None
    assert event.thread_id == ""


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


def test_lark_chromo_treats_invalid_or_non_object_content_as_text() -> None:
    chromo = LarkChromo()

    for content_json in ("{bad json", '["not", "an", "object"]'):
        segments = chromo.parse_segments("text", content_json, None)

        assert len(segments) == 1
        text_segment = segments[0]
        assert isinstance(text_segment, TextSegment)
        assert text_segment.data["text"] == content_json


def test_lark_chromo_renders_markdown_as_interactive_message() -> None:
    chromo = LarkChromo()

    messages = chromo.outbound_markdown("hello **lark**")

    assert len(messages) == 1
    assert messages[0].msg_type == "interactive"
    content = _loaded_json_object(messages[0].content)
    body = content["body"]
    assert isinstance(body, dict)
    assert body["elements"] == [{"tag": "markdown", "content": "hello **lark**"}]


def test_lark_chromo_builds_streaming_card_payload() -> None:
    chromo = LarkChromo()

    data = _loaded_json_object(chromo.make_stream_card_data("hello"))
    message = chromo.make_stream_card_message("card-1")

    assert data["schema"] == "2.0"
    config = data["config"]
    assert isinstance(config, dict)
    assert config["streaming_mode"] is True
    streaming_config = config["streaming_config"]
    assert isinstance(streaming_config, dict)
    print_frequency = streaming_config["print_frequency_ms"]
    assert isinstance(print_frequency, dict)
    print_step = streaming_config["print_step"]
    assert isinstance(print_step, dict)
    assert print_frequency["default"] == 20
    assert print_step["default"] == 12
    body = data["body"]
    assert isinstance(body, dict)
    assert body["elements"] == [
        {
            "tag": "markdown",
            "content": "hello",
            "element_id": "octomate_answer",
        }
    ]
    assert _loaded_json_object(message.content) == {
        "type": "card",
        "data": {"card_id": "card-1"},
    }


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
        self.fail_stream_create = False
        self.fail_stream_update = False

    async def upload_media(self, data: bytes) -> str | None:
        self.uploaded.append(data)
        return f"img-key-{len(self.uploaded)}"

    async def _create_message(
        self,
        receive_id: str,
        receive_id_type: str,
        msg_type: str,
        content: str,
    ) -> str:
        self.created.append((receive_id, receive_id_type, msg_type, content))
        return f"created-{len(self.created)}"

    async def reply_message(
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
    channel.config = LarkChannelConfig(
        app_id="cli-test",
        app_secret=SecretStr("secret"),
        stream=LarkStreamConfig(flush_interval=0.2, min_chars=1),
    )
    _compose_lark_feelers(channel)
    return channel


def _enable_lark_stream(channel: LarkTentacle, *, interval: float = 0.2) -> None:
    channel.config.stream = LarkStreamConfig(flush_interval=interval, min_chars=1)
    _compose_lark_feelers(channel)


def _compose_lark_feelers(channel: LarkTentacle) -> None:
    ink = channel.ink
    chromo = channel.chromo
    markdown_feeler = LarkMarkdownFeeler(ink=ink, chromo=chromo)
    channel.feelers = Feelers(
        markdown=markdown_feeler,
        markdown_stream=LarkMarkdownStreamFeeler(
            ink=ink,
            chromo=chromo,
            stream_config=channel.config.stream,
            markdown_feeler=markdown_feeler,
            channel_id=channel.id,
        ),
        timeline=LarkTimelineFeeler(
            ink=ink, chromo=chromo, stream_config=channel.config.stream
        ),
        approvals=LarkApprovalFeeler(ink),
        ask_questions=LarkAskQuestionFeeler(ink),
    )


async def test_lark_tentacle_replies_to_key_thread_id_when_it_is_open_message_id() -> (
    None
):
    ink = FakeLarkInk()
    channel = _lark_channel(ink)
    key = ConversationKey(
        channel_tentacle_id="lark",
        chat_type="group",
        chat_id="oc_group",
        user_id="ou_user",
        thread_id="om_child_message",
    )

    await channel.feelers.markdown.present(key, "thread reply")

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

    await channel.feelers.markdown.present(key, "private thread reply")

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

    await channel.feelers.markdown.present(key, "new message")

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
    await channel.feelers.markdown_stream.present(
        key,
        streamed_result("hello", "he", "llo"),
    )

    assert len(ink.stream_cards) == 1
    stream_data = _loaded_json_object(ink.stream_cards[0][0])
    stream_config = stream_data["config"]
    assert isinstance(stream_config, dict)
    assert stream_config["streaming_mode"] is True
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
    assert ink.created == []


async def test_lark_present_output_renders_card_updates() -> None:
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

    await channel.feelers.markdown_stream.present_output(
        key,
        _output_events("he", "llo"),
    )

    assert ink.stream_updates == [
        (
            LarkStreamCard(card_id="card-1", element_id="octomate_answer"),
            "hello",
            1,
        )
    ]
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

    await channel.feelers.markdown_stream.present(
        key,
        streamed_result("hello", "he", "llo"),
    )

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

    await channel.feelers.markdown_stream.present(
        key,
        streamed_result("hello", "hello"),
    )

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
    await channel.feelers.markdown_stream.present(
        key,
        streamed_result(
            DeferredToolRequests(
                calls=[
                    ToolCallPart(
                        tool_name="ask_user",
                        args={"question": "Continue?"},
                        tool_call_id="call_1",
                    )
                ]
            )
        ),
    )

    assert ink.stream_updates == []
    assert ink.created == []
    assert ink.replies == []


async def test_lark_tentacle_message_callback_invokes_ingest() -> None:
    channel = object.__new__(LarkTentacle)
    raw = P2ImMessageReceiveV1()
    calls: list[P2ImMessageReceiveV1] = []
    done = asyncio.Event()

    async def ingest(raw: P2ImMessageReceiveV1) -> None:
        calls.append(raw)
        done.set()

    channel.ingest = ingest

    channel.sense(raw)
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


def test_slack_chromo_renders_markdown_result() -> None:
    chromo = SlackChromo()
    markdown = (
        "# Hello Slack\n\n"
        "Keep **bold**, [links](https://example.com), and tables intact.\n\n"
        "| a | b |\n| - | - |\n| 1 | 2 |"
    )

    messages = chromo.outbound_markdown(markdown)

    assert len(messages) == 1
    assert messages[0].text == markdown
    assert messages[0].markdown_text == markdown
    assert messages[0].blocks is None


@dataclass
class FakeSlackStream:
    pass


@dataclass
class FakeSlackInk(Ink[SlackOutboundMessage]):
    streams: list[dict[str, str | None]] = field(default_factory=list)
    appends: list[str] = field(default_factory=list)
    stream_chunks: list[list[Chunk]] = field(default_factory=list)
    stops: list[str | None] = field(default_factory=list)
    finals: list[dict[str, str | None]] = field(default_factory=list)
    statuses: list[str] = field(default_factory=list)
    sent: list[tuple[str, str, list[SlackOutboundMessage], str | None]] = field(
        default_factory=list
    )
    uploads: list[tuple[str, bytes, str, str | None]] = field(default_factory=list)

    def inspect(self) -> UserProfile:
        return UserProfile(user_id="bot", name="Bot")

    async def get_user_profile(self, user_id: str) -> UserProfile:
        return UserProfile(user_id=user_id, name=user_id)

    async def upload_media(self, data: bytes) -> str | None:
        return None

    async def upload_image(
        self,
        *,
        channel: str,
        data: bytes,
        filename: str,
        thread_ts: str | None = None,
    ) -> str | None:
        self.uploads.append((channel, data, filename, thread_ts))
        return f"file-{len(self.uploads)}"

    async def download_image(
        self,
        seg: ImageSegment,
        message_id: str,
    ) -> DownloadedImage | None:
        return None

    async def start_stream(
        self,
        channel: str,
        thread_ts: str,
        *,
        recipient_user_id: str | None = None,
        recipient_team_id: str | None = None,
        task_display_mode: str | None = None,
    ) -> FakeSlackStream:
        stream = {
            "channel": channel,
            "thread_ts": thread_ts,
            "recipient_user_id": recipient_user_id,
            "recipient_team_id": recipient_team_id,
        }
        if task_display_mode is not None:
            stream["task_display_mode"] = task_display_mode
        self.streams.append(stream)
        return FakeSlackStream()

    @asynccontextmanager
    async def open_stream(
        self,
        channel: str,
        thread_ts: str,
        *,
        recipient_user_id: str | None = None,
        recipient_team_id: str | None = None,
        task_display_mode: str | None = None,
    ) -> AsyncIterator[FakeSlackStream]:
        stream = await self.start_stream(
            channel,
            thread_ts,
            recipient_user_id=recipient_user_id,
            recipient_team_id=recipient_team_id,
            task_display_mode=task_display_mode,
        )
        try:
            yield stream
        finally:
            await self.stop_stream(stream)

    async def append_stream(self, stream: FakeSlackStream, markdown_text: str) -> None:
        self.appends.append(markdown_text)

    async def append_stream_chunks(
        self,
        stream: FakeSlackStream,
        chunks: list[Chunk],
    ) -> None:
        self.stream_chunks.append(chunks)

    async def stop_stream(
        self,
        stream: FakeSlackStream,
        *,
        markdown_text: str | None = None,
    ) -> str:
        self.stops.append(markdown_text)
        return "stream-ts"

    async def set_assistant_status(
        self,
        channel: str,
        thread_ts: str,
        status: str,
    ) -> None:
        self.statuses.append(status)

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
        reply_in_thread: bool = False,
    ) -> str:
        self.sent.append((chat_id, chat_type, messages, reply_to))
        return "fallback-ts"


def _slack_channel(ink: FakeSlackInk) -> SlackTentacle:
    channel = object.__new__(SlackTentacle)
    channel.id = "slack"
    channel.ink = cast(SlackInkType, ink)
    channel.chromo = SlackChromo()
    channel.config = SlackChannelConfig(
        app_id="A-test",
        bot_token=SecretStr("xoxb-test"),
        app_token=SecretStr("xapp-test"),
        stream=SlackStreamConfig(flush_interval=0),
    )
    _compose_slack_feelers(channel)
    return channel


def _compose_slack_feelers(channel: SlackTentacle) -> None:
    ink = cast(SlackInkType, channel.ink)
    chromo = cast(SlackChromo, channel.chromo)
    markdown_feeler = DefaultMarkdownFeeler(ink=ink, chromo=chromo)
    channel.feelers = Feelers(
        markdown=markdown_feeler,
        markdown_stream=SlackMarkdownStreamFeeler(
            ink=ink,
            chromo=chromo,
            stream_config=channel.config.stream,
            markdown_feeler=markdown_feeler,
            channel_id=channel.id,
        ),
        timeline=SlackTimelineFeeler(ink=ink, chromo=chromo),
        approvals=SlackApprovalFeeler(ink),
        ask_questions=SlackAskQuestionFeeler(ink),
    )


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

    await channel.feelers.markdown_stream.present(
        _slack_key(),
        streamed_result("# Hello\n**world**", "# Hello\n", "**world**"),
    )

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


async def test_slack_present_output_renders_output_event_deltas() -> None:
    ink = FakeSlackInk()
    channel = _slack_channel(ink)

    await channel.feelers.markdown_stream.present_output(
        _slack_key(),
        _output_events("# Hello\n", "**world**"),
    )

    assert ink.appends == ["# Hello\n", "**world**"]
    assert ink.stops == [None]


async def test_slack_tentacle_can_batch_stream_deltas_when_configured() -> None:
    ink = FakeSlackInk()
    channel = _slack_channel(ink)
    channel.config.stream = SlackStreamConfig(flush_interval=999, min_chars=100)
    _compose_slack_feelers(channel)

    await channel.feelers.markdown_stream.present(
        _slack_key(),
        streamed_result("# Hello\n**world**", "# Hello\n", "**world**"),
    )

    assert ink.appends == ["# Hello\n**world**"]
    assert ink.stops == [None]


async def test_slack_tentacle_stops_stream_on_deferred_result() -> None:
    ink = FakeSlackInk()
    channel = _slack_channel(ink)

    await channel.feelers.markdown_stream.present(
        _slack_key(),
        streamed_result(
            DeferredToolRequests(
                calls=[
                    ToolCallPart(
                        tool_name="ask_user",
                        args={"question": "Continue?"},
                        tool_call_id="call_1",
                    )
                ]
            )
        ),
    )

    assert ink.appends == []
    assert ink.sent == []


async def test_slack_consume_renders_timeline_per_event() -> None:
    ink = FakeSlackInk()
    channel = _slack_channel(ink)

    async def events() -> AsyncIterator[
        StreamEvents[ChannelOutput] | AgentRunResultEvent[ChannelOutput]
    ]:
        yield PartStartEvent(index=0, part=ThinkingPart(content="checking"))
        # ask_questions is skipped from the timeline (call + result).
        yield FunctionToolCallEvent(
            ToolCallPart(tool_name="ask_questions", args={}, tool_call_id="call_q")
        )
        yield FunctionToolResultEvent(
            ToolReturnPart(
                tool_name="ask_questions", content={"ok": True}, tool_call_id="call_q"
            )
        )
        yield FunctionToolCallEvent(
            ToolCallPart(tool_name="lookup", args={"query": "x"}, tool_call_id="call_1")
        )
        yield FunctionToolResultEvent(
            ToolReturnPart(
                tool_name="lookup", content={"ok": True}, tool_call_id="call_1"
            )
        )
        # The answer streams as typed deltas, not raw text parts.
        yield ResultTextDeltaEvent(delta="done")
        yield FinalResult[ChannelOutput](output="done")
        yield AgentRunResultEvent(AgentRunResult("done"))

    message_id = await channel.consume(_slack_key(), events())

    assert message_id == "stream-ts"
    assert ink.streams and ink.streams[0]["task_display_mode"] == "timeline"
    chunks = [chunk for group in ink.stream_chunks for chunk in group]
    task_chunks = [chunk for chunk in chunks if isinstance(chunk, TaskUpdateChunk)]
    # ask_questions skipped; thinking + lookup rendered.
    assert {chunk.id for chunk in task_chunks} == {"thinking-1", "call_1"}
    assert any(chunk.title == "Thinking" for chunk in task_chunks)
    assert any(chunk.title == "Lookup" for chunk in task_chunks)
    details = "\n\n".join(chunk.details or "" for chunk in task_chunks)
    assert "ask_questions" not in details
    assert ink.appends == ["done"]
    assert ink.stops == ["stream-ts"] or ink.stops == [None]
    assert ink.statuses[0] == "Thinking…"
    assert "Lookup…" in ink.statuses
    assert "Writing the response…" in ink.statuses
    assert ink.statuses[-1] == ""


async def test_lark_consume_renders_timeline_per_event() -> None:
    ink = FakeLarkInk()
    channel = _lark_channel(ink)
    key = ConversationKey(
        channel_tentacle_id="lark",
        chat_type="private",
        chat_id="u1",
        user_id="u1",
    )

    async def events() -> AsyncIterator[
        StreamEvents[ChannelOutput] | AgentRunResultEvent[ChannelOutput]
    ]:
        yield PartStartEvent(index=0, part=ThinkingPart(content="checking"))
        yield FunctionToolCallEvent(
            ToolCallPart(tool_name="lookup", args={"query": "x"}, tool_call_id="call_1")
        )
        yield FunctionToolResultEvent(
            ToolReturnPart(
                tool_name="lookup", content={"ok": True}, tool_call_id="call_1"
            )
        )
        # The answer streams as raw text parts (the react graph, not the normalizer).
        yield PartStartEvent(index=1, part=TextPart(content="done"))
        yield AgentRunResultEvent(AgentRunResult("done"))

    message_id = await channel.consume(key, events())

    # Thinking + tool each posted as their own card (then folded via patch)...
    assert len(ink.created) == 2
    assert len(ink.patched) == 2
    # ...and the answer streamed into its own stream card, then finalized.
    assert ink.stream_cards
    assert any("done" in content for _card, content, _seq in ink.stream_updates)
    assert ink.finalized
    assert message_id == "stream-1"


async def test_slack_consume_renders_image_and_card_segments(
    tmp_path: Path,
) -> None:
    ink = FakeSlackInk()
    channel = _slack_channel(ink)
    image_path = tmp_path / "reef.png"
    image_path.write_bytes(b"png-bytes")

    async def events() -> AsyncIterator[
        StreamEvents[ChannelOutput] | AgentRunResultEvent[ChannelOutput]
    ]:
        yield ResultSegmentEvent(segment=MarkdownSegment(data={"text": "see:"}))
        yield ResultSegmentEvent(
            segment=ImageSegment(data=ImageData(file=str(image_path)))
        )
        yield ResultSegmentEvent(
            segment=CardSegment(data=CardData(payload={"blocks": [{"type": "divider"}]}))
        )

    await channel.consume(_slack_key(), events())

    # Markdown streams as answer text; the image uploads shared into the thread;
    # the card posts as a blocks message.
    assert ink.appends == ["see:"]
    assert ink.uploads == [("C1", b"png-bytes", "reef.png", "1710000000.000100")]
    sent_channel, _chat_type, messages, reply_to = ink.sent[0]
    assert sent_channel == "C1"
    assert reply_to == "1710000000.000100"
    assert messages[0].blocks == [{"type": "divider"}]


async def test_lark_consume_renders_image_and_card_segments(
    tmp_path: Path,
) -> None:
    ink = FakeLarkInk()
    channel = _lark_channel(ink)
    key = ConversationKey(
        channel_tentacle_id="lark",
        chat_type="private",
        chat_id="u1",
        user_id="u1",
    )
    image_path = tmp_path / "reef.png"
    image_path.write_bytes(b"png-bytes")

    async def events() -> AsyncIterator[
        StreamEvents[ChannelOutput] | AgentRunResultEvent[ChannelOutput]
    ]:
        yield ResultSegmentEvent(
            segment=ImageSegment(data=ImageData(file=str(image_path)))
        )
        yield ResultSegmentEvent(
            segment=CardSegment(data=CardData(payload={"header": {"title": "t"}}))
        )

    await channel.consume(key, events())

    # The image uploads then sends as an image message; the card posts as an
    # interactive message carrying the payload verbatim.
    assert ink.uploaded == [b"png-bytes"]
    image_contents = [
        content for _, _, msg_type, content in ink.created if msg_type == "image"
    ]
    assert [json.loads(content) for content in image_contents] == [
        {"image_key": "img-key-1"}
    ]
    interactive_contents = [
        content for _, _, msg_type, content in ink.created if msg_type == "interactive"
    ]
    assert json.loads(interactive_contents[-1]) == {"header": {"title": "t"}}


async def test_slack_consume_renders_todos_as_timeline_tasks() -> None:
    ink = FakeSlackInk()
    channel = _slack_channel(ink)
    todo = Todo(conversation_id=uuid4(), ref="T1", content="Find the docs")

    async def events() -> AsyncIterator[
        StreamEvents[ChannelOutput] | AgentRunResultEvent[ChannelOutput]
    ]:
        yield TodoCreatedEvent(todo=todo)
        yield TodoStatusChangedEvent(
            todo=todo.model_copy(
                update={"status": "in_progress", "active_form": "Finding the docs"}
            ),
            previous=todo,
        )
        yield TodoCompletedEvent(todo=todo.model_copy(update={"status": "completed"}))

    await channel.consume(_slack_key(), events())

    chunks = [chunk for group in ink.stream_chunks for chunk in group]
    todo_chunks = [
        chunk
        for chunk in chunks
        if isinstance(chunk, TaskUpdateChunk) and chunk.id == "todo-T1"
    ]
    assert [chunk.status for chunk in todo_chunks] == [
        "pending",
        "in_progress",
        "complete",
    ]
    # The spinner shows the active form; done shows the content again.
    assert todo_chunks[1].title == "Finding the docs"
    assert todo_chunks[2].title == "Find the docs"


async def test_lark_consume_renders_todo_checklist_card() -> None:
    ink = FakeLarkInk()
    channel = _lark_channel(ink)
    key = ConversationKey(
        channel_tentacle_id="lark",
        chat_type="private",
        chat_id="u1",
        user_id="u1",
    )
    todo = Todo(conversation_id=uuid4(), ref="T1", content="Find the docs")

    async def events() -> AsyncIterator[
        StreamEvents[ChannelOutput] | AgentRunResultEvent[ChannelOutput]
    ]:
        yield TodoCreatedEvent(todo=todo)
        yield TodoCompletedEvent(todo=todo.model_copy(update={"status": "completed"}))

    await channel.consume(key, events())

    # First event posts the checklist card; later events patch it in place.
    todo_cards = [
        content for _, _, msg_type, content in ink.created if "Tasks" in content
    ]
    assert todo_cards
    assert "- [ ] Find the docs" in todo_cards[0]
    assert ink.patched
    assert "- [x] Find the docs" in ink.patched[-1][1]


async def test_slack_tentacle_streams_final_only_result_once() -> None:
    ink = FakeSlackInk()
    channel = _slack_channel(ink)

    await channel.feelers.markdown_stream.present(
        _slack_key(),
        streamed_result("final **markdown**"),
    )

    assert len(ink.streams) == 1
    assert ink.appends == ["final **markdown**"]
    assert ink.stops == [None]
    assert ink.finals == []


async def test_slack_tentacle_closes_open_stream_on_append_error() -> None:
    class RaisingSlackInk(FakeSlackInk):
        async def append_stream(
            self,
            stream: FakeSlackStream,
            markdown_text: str,
        ) -> None:
            await super().append_stream(stream, markdown_text)
            raise RuntimeError("append failed")

    ink = RaisingSlackInk()
    channel = _slack_channel(ink)

    await channel.feelers.markdown_stream.present(
        _slack_key(),
        streamed_result("hello"),
    )

    assert ink.appends == ["hello"]
    assert ink.stops == [None]


class FakeSlackClient:
    def __init__(self) -> None:
        self.streams: list[JsonObject] = []
        self.uploads: list[JsonObject] = []

    async def chat_stream(
        self,
        *,
        buffer_size: int,
        channel: str,
        thread_ts: str,
        recipient_user_id: str | None = None,
        recipient_team_id: str | None = None,
        task_display_mode: str | None = None,
    ) -> AsyncChatStream:
        self.streams.append(
            {
                "buffer_size": buffer_size,
                "channel": channel,
                "thread_ts": thread_ts,
                "recipient_user_id": recipient_user_id,
                "recipient_team_id": recipient_team_id,
                "task_display_mode": task_display_mode,
            }
        )
        return cast(AsyncChatStream, SimpleNamespace())

    async def files_upload_v2(
        self,
        *,
        channel: str,
        content: str,
        filename: str,
        title: str,
        snippet_type: str,
        initial_comment: str,
        thread_ts: str | None = None,
    ) -> JsonObject:
        self.uploads.append(
            {
                "channel": channel,
                "content": content,
                "filename": filename,
                "title": title,
                "snippet_type": snippet_type,
                "initial_comment": initial_comment,
                "thread_ts": thread_ts,
            }
        )
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
            self.appends: list[dict[str, str | tuple[()]]] = []

        async def append(
            self,
            *,
            markdown_text: str,
            chunks: tuple[()] = (),
        ) -> None:
            self.appends.append({"markdown_text": markdown_text, "chunks": chunks})

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
        ) -> Conversation:
            self.calls.append((key, agent_tentacle_id))
            return cast(Conversation, SimpleNamespace())

    conversations = FakeConversations()
    channel = _slack_channel(FakeSlackInk())
    channel.octomate = cast(Octomate, SimpleNamespace(conversations=conversations))
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
