from __future__ import annotations

import json

from octomate.schemas.segments import (
    AtSegment,
    ImageSegment,
    ReplySegment,
    TextSegment,
)
from octomate.tentacles.channel.napcat import NapcatChromo
from octomate.tentacles.channel.napcat.schema import NapcatOutboundMessage


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
