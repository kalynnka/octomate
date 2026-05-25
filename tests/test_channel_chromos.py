from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any
from typing import cast

from pydantic import BaseModel
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

from octomate.schemas.conversation import ConversationKey
from octomate.schemas.events import MessageEvent
from octomate.schemas.segments import AtSegment, ImageSegment, ReplySegment, TextSegment
from octomate.tentacles.channel.base import MarkdownChunker
from octomate.tentacles.channel.lark import LarkChromo, LarkInk, LarkTentacle
from octomate.tentacles.channel.lark.schema import LarkOutboundMessage
from octomate.tentacles.channel.slack import SlackChromo, SlackInk, SlackTentacle
from octomate.tentacles.channel.slack.ink import (
    SLACK_MARKDOWN_TEXT_LIMIT,
)
from octomate.tentacles.channel.slack.ink import SlackInk as SlackInkType
from octomate.tentacles.channel.slack.schema import SlackOutboundMessage

SlackEvent = AgentStreamEvent | AgentRunResultEvent[str]


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
) -> SimpleNamespace:
    return SimpleNamespace(
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
    assert event.segments[2].data.user_id == "ou_mentioned"


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
    assert event.segments[0].data.file == "img_key"


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
    assert event.segments[3].data.user_id == "ou_reviewer"
    assert event.segments[4].data.file == "img_post"


async def test_lark_chromo_renders_final_text_as_interactive_markdown() -> None:
    chromo = LarkChromo()

    async def events() -> AsyncIterator[AgentRunResultEvent[str]]:
        yield AgentRunResultEvent(AgentRunResult("hello **lark**"))

    messages = [message async for message in chromo.squirt(events())]

    assert len(messages) == 1
    assert messages[0].msg_type == "interactive"
    content = json.loads(messages[0].content)
    assert content["body"]["elements"] == [
        {"tag": "markdown", "content": "hello **lark**"}
    ]


async def test_lark_chromo_renders_structured_output_as_json_card() -> None:
    class Answer(BaseModel):
        ok: bool
        count: int

    chromo = LarkChromo()

    async def events() -> AsyncIterator[AgentRunResultEvent[Answer]]:
        yield AgentRunResultEvent(AgentRunResult(Answer(ok=True, count=2)))

    messages = [message async for message in chromo.squirt(events())]
    content = json.loads(messages[0].content)
    markdown = content["body"]["elements"][0]["content"]

    assert markdown.startswith("```json")
    assert '"ok": true' in markdown
    assert '"count": 2' in markdown


async def test_lark_chromo_renders_deferred_requests_as_markdown_card() -> None:
    chromo = LarkChromo()

    async def events() -> AsyncIterator[AgentRunResultEvent[DeferredToolRequests]]:
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

    messages = [message async for message in chromo.squirt(events())]
    content = json.loads(messages[0].content)
    markdown = content["body"]["elements"][0]["content"]

    assert "`ask_user` needs input" in markdown
    assert "`call_1`" in markdown


class FakeLarkInk(LarkInk):
    def __init__(self) -> None:
        self.created: list[tuple[str, str, str, str]] = []
        self.replies: list[tuple[str, str, str, bool]] = []

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
    return channel


async def test_lark_tentacle_replies_to_source_message_not_thread_id() -> None:
    ink = FakeLarkInk()
    channel = _lark_channel(ink)
    key = ConversationKey(
        channel_tentacle_id="lark",
        chat_type="group",
        chat_id="oc_group",
        user_id="ou_user",
        thread_id="omt_19766a1bf00edb8e",
    )
    source = MessageEvent(
        message_id="om_child_message",
        thread_id="omt_19766a1bf00edb8e",
        user_id="ou_user",
        chat_id="oc_group",
        chat_type="group",
    )

    async def events() -> AsyncIterator[AgentRunResultEvent[str]]:
        yield AgentRunResultEvent(AgentRunResult("thread reply"))

    await channel.respond(key, events(), source_events=[source])

    assert ink.created == []
    assert len(ink.replies) == 1
    message_id, msg_type, content, reply_in_thread = ink.replies[0]
    assert message_id == "om_child_message"
    assert msg_type == "interactive"
    assert "thread reply" in content
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

    async def events() -> AsyncIterator[AgentRunResultEvent[str]]:
        yield AgentRunResultEvent(AgentRunResult("new message"))

    await channel.respond(key, events(), source_events=[])

    assert ink.replies == []
    assert ink.created[0][:2] == ("oc_group", "chat_id")


async def test_lark_tentacle_message_callback_invokes_ingest() -> None:
    channel = object.__new__(LarkTentacle)
    raw = object()
    calls: list[object] = []
    done = asyncio.Event()

    async def ingest(data: object) -> None:
        calls.append(data)
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


async def test_slack_chromo_renders_final_text_result() -> None:
    chromo = SlackChromo()
    markdown = (
        "# Hello Slack\n\n"
        "Keep **bold**, [links](https://example.com), and tables intact.\n\n"
        "| a | b |\n| - | - |\n| 1 | 2 |"
    )

    async def events() -> AsyncIterator[AgentRunResultEvent[str]]:
        yield AgentRunResultEvent(AgentRunResult(markdown))

    messages = [message async for message in chromo.squirt(events())]

    assert len(messages) == 1
    assert messages[0].text == markdown
    assert messages[0].markdown_text == markdown
    assert messages[0].blocks is None


async def test_slack_chromo_renders_structured_output_as_json() -> None:
    class Answer(BaseModel):
        ok: bool
        count: int

    chromo = SlackChromo()

    async def events() -> AsyncIterator[AgentRunResultEvent[Answer]]:
        yield AgentRunResultEvent(AgentRunResult(Answer(ok=True, count=2)))

    messages = [message async for message in chromo.squirt(events())]

    assert len(messages) == 1
    assert messages[0].text.startswith("```json")
    assert messages[0].markdown_text == messages[0].text
    assert '"ok": true' in messages[0].text
    assert '"count": 2' in messages[0].text


async def test_slack_chromo_renders_deferred_requests_as_markdown() -> None:
    chromo = SlackChromo()

    async def events() -> AsyncIterator[AgentRunResultEvent[DeferredToolRequests]]:
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

    messages = [message async for message in chromo.squirt(events())]

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
    return channel


def _slack_key() -> ConversationKey:
    return ConversationKey(
        channel_tentacle_id="slack",
        chat_type="group",
        chat_id="C1",
        user_id="U1",
    )


def _source_event() -> MessageEvent:
    return MessageEvent(
        message_id="1710000000.000100",
        user_id="U1",
        chat_id="C1",
        chat_type="group",
        raw=json.dumps(
            {
                "ts": "1710000000.000100",
                "user": "U1",
                "channel": "C1",
                "team": "T1",
            }
        ),
    )


async def test_slack_tentacle_streams_text_deltas_in_source_thread() -> None:
    ink = FakeSlackInk()
    channel = _slack_channel(ink)

    async def events() -> AsyncIterator[SlackEvent]:
        yield PartStartEvent(index=0, part=TextPart(content="# Hello\n"))
        yield PartDeltaEvent(index=0, delta=TextPartDelta(content_delta="**world**"))
        yield AgentRunResultEvent(AgentRunResult("# Hello\n**world**"))

    await channel.respond(_slack_key(), events(), source_events=[_source_event()])

    assert ink.streams == [
        {
            "channel": "C1",
            "thread_ts": "1710000000.000100",
            "recipient_user_id": "U1",
            "recipient_team_id": "T1",
        }
    ]
    assert ink.appends == ["# Hello\n", "**world**"]
    assert ink.stops == [None]
    assert ink.finals == []


async def test_slack_tentacle_streams_final_only_result_once() -> None:
    ink = FakeSlackInk()
    channel = _slack_channel(ink)

    async def events() -> AsyncIterator[SlackEvent]:
        yield AgentRunResultEvent(AgentRunResult("final **markdown**"))

    await channel.respond(_slack_key(), events(), source_events=[_source_event()])

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

    await channel.respond(_slack_key(), events(), source_events=[_source_event()])

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
    channel.octomate = SimpleNamespace(conversations=conversations)
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
