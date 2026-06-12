"""SlackInk transport behavior over a fake web client."""

from __future__ import annotations

from typing import cast

from slack_sdk.web.async_chat_stream import AsyncChatStream
from slack_sdk.web.async_client import AsyncWebClient

from octomate.tentacles.channel.slack.ink import SLACK_MARKDOWN_TEXT_LIMIT, SlackInk

from tests.channels.slack.fakes import FakeSlackClient


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
