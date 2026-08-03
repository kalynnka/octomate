from __future__ import annotations

import base64
import json
from pathlib import Path

import pytest

from octomate.schemas.segments import (
    AtSegment,
    CardData,
    CardSegment,
    FileData,
    FileSegment,
    ImageData,
    ImageSegment,
    MarkdownSegment,
    MessageSegment,
    ReplySegment,
    TextSegment,
)
from octomate.tentacles.channels.napcat import NapcatChromo
from octomate.tentacles.channels.napcat.schema import NapcatOutboundMessage


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
                        "data": {"file": "image-address", "url": "https://image"},
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
    assert image_seg.data.file == "image-address"


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


async def test_napcat_chromo_outbound_segments_native_media(tmp_path: Path) -> None:
    chromo = NapcatChromo()
    image = tmp_path / "pic.png"
    image.write_bytes(b"image-bytes")
    doc = tmp_path / "report.pdf"
    doc.write_bytes(b"file-bytes")

    segments: list[MessageSegment] = [
        TextSegment(data={"text": "before"}),
        MarkdownSegment(data={"text": "and **after**"}),
        ImageSegment(data=ImageData(file=str(image))),
        FileSegment(data=FileData(file=str(doc), name="report.pdf")),
        CardSegment(data=CardData(payload={})),
    ]
    messages = await chromo.outbound_segments(segments)

    image_b64 = base64.b64encode(b"image-bytes").decode()
    file_b64 = base64.b64encode(b"file-bytes").decode()
    assert messages == [
        NapcatOutboundMessage(
            segments=[
                {"type": "text", "data": {"text": "before"}},
                {"type": "text", "data": {"text": "and after"}},
                {"type": "image", "data": {"file": f"base64://{image_b64}"}},
                {
                    "type": "file",
                    "data": {"file": f"base64://{file_b64}", "name": "report.pdf"},
                },
                {"type": "text", "data": {"text": "[card]"}},
            ]
        )
    ]


async def test_napcat_chromo_outbound_segments_skips_empty_and_returns_nothing() -> (
    None
):
    chromo = NapcatChromo()

    assert await chromo.outbound_segments([TextSegment(data={"text": ""})]) == []
    assert await chromo.outbound_segments([]) == []


async def test_napcat_chromo_outbound_segments_missing_file_raises(
    tmp_path: Path,
) -> None:
    chromo = NapcatChromo()
    missing = tmp_path / "gone.png"

    segments: list[MessageSegment] = [ImageSegment(data=ImageData(file=str(missing)))]
    with pytest.raises(FileNotFoundError):
        await chromo.outbound_segments(segments)
