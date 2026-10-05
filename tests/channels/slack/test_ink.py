"""SlackInk transport behavior over a fake web client."""

from __future__ import annotations

from collections.abc import AsyncGenerator
from typing import cast

import pytest
from slack_sdk.errors import SlackApiError
from slack_sdk.web.async_chat_stream import AsyncChatStream
from slack_sdk.web.async_client import AsyncWebClient

from octomate.schemas.conversation import ChannelAddress
from octomate.tentacles.slack.ink import SLACK_MARKDOWN_TEXT_LIMIT, SlackInk
from octomate.tentacles.slack.schema import SlackOutboundMessage
from tests.channels.slack.fakes import FakeSlackClient


async def test_slack_ink_uses_external_destination_as_thread_only_on_threads() -> None:
    client = FakeSlackClient()
    ink = object.__new__(SlackInk)
    ink.client = cast(AsyncWebClient, client)
    message = SlackOutboundMessage(text="hello")

    group_id = await ink.send_message("C1", "group", [message], channel_thread_id="C1")
    thread_id = await ink.send_message(
        "C1", "thread", [message], channel_thread_id="1710000000.000100"
    )

    assert group_id == "message-1"
    assert thread_id == "message-2"
    assert client.messages == [
        {"channel": "C1", "text": "hello"},
        {
            "channel": "C1",
            "text": "hello",
            "thread_ts": "1710000000.000100",
        },
    ]


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


class ListingSlackClient:
    """`users.conversations` for the bot and for one user, a page at a time."""

    def __init__(self, bot: list[list[str]], user: list[list[str]]) -> None:
        self.pages = {None: bot, "alice": user}

    async def users_conversations(
        self, *, user: str | None = None, **kwargs: str | int | bool
    ) -> AsyncGenerator[dict[str, list[dict[str, str]]]]:
        async def pages() -> AsyncGenerator[dict[str, list[dict[str, str]]]]:
            for page in self.pages[user]:
                yield {"channels": [{"id": one, "name": one.lower()} for one in page]}

        return pages()


async def test_slack_lists_and_accepts_only_channels_the_bot_and_user_share() -> None:
    ink = object.__new__(SlackInk)
    ink.client = cast(
        AsyncWebClient,
        ListingSlackClient(bot=[["C1", "C2"], ["C3"]], user=[["C3"], ["C1", "C4"]]),
    )
    dm = ChannelAddress("slack", "dm", "", "alice")

    listed = await ink.list_addresses(dm)

    assert listed == [
        ChannelAddress("slack", "group", "C1", "alice", shared=True),
        ChannelAddress("slack", "group", "C3", "alice", shared=True),
    ]
    assert [one.metadata for one in listed] == [{"name": "c1"}, {"name": "c3"}]
    # The address is the client's to send, so who can read it is not its to say,
    # and a private conversation lands in a channel's thread just as publicly.
    claimed = ChannelAddress("slack", "group", "C3", "alice")
    assert (await ink.prepare_address(claimed, private=True)).shared
    for elsewhere in ("C2", "C4"):
        with pytest.raises(ValueError, match="not both in"):
            await ink.prepare_address(
                ChannelAddress("slack", "group", elsewhere, "alice")
            )
    with pytest.raises(ValueError, match="nothing to open"):
        await ink.list_addresses(dm, "C1")


async def test_slack_says_which_scope_a_refused_listing_needs() -> None:
    class RefusingSlackClient:
        async def users_conversations(self, **kwargs: str | int | bool) -> None:
            raise SlackApiError(
                "refused", {"error": "missing_scope", "needed": "channels:read"}
            )

    ink = object.__new__(SlackInk)
    ink.client = cast(AsyncWebClient, RefusingSlackClient())

    with pytest.raises(ValueError, match="missing_scope, needs channels:read"):
        await ink.list_addresses(ChannelAddress("slack", "dm", "", "alice"))
