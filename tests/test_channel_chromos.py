from __future__ import annotations

import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
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
from octomate.schemas.segments import AtSegment, ImageSegment, TextSegment
from octomate.tentacles.channel.base import MarkdownChunker
from octomate.tentacles.channel.slack import SlackChromo, SlackInk, SlackTentacle
from octomate.tentacles.channel.slack.ink import (
    SLACK_MARKDOWN_TEXT_LIMIT,
)
from octomate.tentacles.channel.slack.ink import SlackInk as SlackInkType
from octomate.tentacles.channel.slack.schema import SlackOutboundMessage

SlackEvent = AgentStreamEvent | AgentRunResultEvent[str]


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
class FakeSlackStreamSession:
    ink: FakeSlackInk
    stream: object
    appended: bool = False
    final_markdown_text: str = ""

    async def append(self, markdown_text: str) -> None:
        self.appended = True
        await self.ink.append_stream(self.stream, markdown_text)

    async def close(self) -> None:
        await self.ink.stop_stream(
            self.stream,
            markdown_text=None if self.appended else self.final_markdown_text or None,
        )


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
    ) -> AsyncIterator[FakeSlackStreamSession]:
        stream = await self.start_stream(
            channel,
            thread_ts,
            recipient_user_id=recipient_user_id,
            recipient_team_id=recipient_team_id,
        )
        session = FakeSlackStreamSession(ink=self, stream=stream)
        try:
            yield session
        finally:
            await session.close()

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
    assert ink.appends == []
    assert ink.stops == ["final **markdown**"]
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
