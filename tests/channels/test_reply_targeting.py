"""Outbound reply-to + mention segments.

`ReplySegment` and `AtSegment` are part of the `MessageSegment` output vocabulary.
The first reply anywhere in the list is threaded as the platform `reply_to` (and
every reply is stripped from the body, never rendered as `[reply: …]` text); a
mention encodes to each platform's native ping token.
"""

from __future__ import annotations

from typing import Any, cast

from octomate.capabilities.events import MessageSentEvent
from octomate.capabilities.send import SendCapability
from octomate.schemas.conversation import ConversationKey
from octomate.schemas.segments import (
    AtData,
    AtSegment,
    MarkdownSegment,
    ReplySegment,
    TextSegment,
)
from octomate.tentacles.channel.feelers.output import DefaultSegmentsFeeler, split_reply
from octomate.tentacles.channel.lark.chromo import LarkChromo
from octomate.tentacles.channel.napcat.chromo import NapcatChromo
from octomate.tentacles.channel.slack.chromo import SlackChromo
from tests.support.channels import FakeChromo, RecordingInk


def _key() -> ConversationKey:
    return ConversationKey(
        channel_tentacle_id="im",
        chat_type="private",
        chat_id="alice",
        user_id="alice",
        thread_id="thread-1",
    )


def test_split_reply_takes_first_reply_anywhere_and_strips_all() -> None:
    body = [TextSegment(data={"text": "hi"}), MarkdownSegment(data={"text": "bye"})]
    # A reply mid-list still wins, and every reply is stripped from the body.
    segments = [
        body[0],
        ReplySegment(data={"id": "msg-7"}),
        body[1],
        ReplySegment(data={"id": "msg-9"}),
    ]
    assert split_reply(segments) == ("msg-7", body)
    # No reply: id is None, body unchanged.
    assert split_reply(body) == (None, body)


async def test_send_message_tool_accepts_reply_and_mention() -> None:
    capability = SendCapability()
    assert capability.toolset is not None
    send_message = capability.toolset.tools["send_message"].function
    segments = [
        ReplySegment(data={"id": "msg-3"}),
        AtSegment(data=AtData(user_id="U42")),
        MarkdownSegment(data={"text": "over to you"}),
    ]

    result = await send_message(cast(Any, None), segments)

    assert result.metadata == [MessageSentEvent(segments=segments)]


async def test_default_segments_feeler_threads_leading_reply() -> None:
    ink = RecordingInk()
    feeler = DefaultSegmentsFeeler(ink=ink, chromo=FakeChromo())

    await feeler.present(
        _key(),
        [
            ReplySegment(data={"id": "msg-42"}),
            MarkdownSegment(data={"text": "answer"}),
        ],
    )

    chat_id, _chat_type, messages, reply_to, _thread = ink.sent[0]
    assert chat_id == "alice"
    assert reply_to == "msg-42"  # the reply id overrides the conversation thread
    assert messages == [{"text": "answer"}]  # body excludes the reply token


async def test_default_segments_feeler_falls_back_to_thread() -> None:
    ink = RecordingInk()
    feeler = DefaultSegmentsFeeler(ink=ink, chromo=FakeChromo())

    await feeler.present(_key(), [MarkdownSegment(data={"text": "answer"})])

    assert ink.sent[0][3] == "thread-1"


async def test_napcat_segments_feeler_threads_leading_reply() -> None:
    ink = RecordingInk()
    feeler = DefaultSegmentsFeeler(ink=cast(Any, ink), chromo=NapcatChromo())

    await feeler.present(
        _key(),
        [
            ReplySegment(data={"id": "999"}),
            TextSegment(data={"text": "hi"}),
        ],
    )

    _chat_id, _chat_type, messages, reply_to, _thread = ink.sent[0]
    assert reply_to == "999"
    assert cast(Any, messages[0]).segments == [
        {"type": "text", "data": {"text": "hi"}}
    ]


async def test_napcat_chromo_renders_native_mention() -> None:
    messages = await NapcatChromo().outbound_segments(
        [AtSegment(data=AtData(user_id="123"))]
    )
    assert messages[0].segments == [{"type": "at", "data": {"qq": "123"}}]


async def test_slack_chromo_renders_native_mention() -> None:
    messages = await SlackChromo().outbound_segments(
        [AtSegment(data=AtData(user_id="U42"))]
    )
    assert messages[0].text == "<@U42>"


async def test_lark_chromo_renders_native_mention() -> None:
    messages = await LarkChromo().outbound_segments(
        [AtSegment(data=AtData(user_id="ou_7"))]
    )
    assert "<at id=ou_7></at>" in messages[0].content
