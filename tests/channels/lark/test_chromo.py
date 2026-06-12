"""Lark chromo decode (inbound events) and encode (outbound payloads) tests."""

from __future__ import annotations

import json

from lark_oapi.api.im.v1.model.p2_im_message_receive_v1 import P2ImMessageReceiveV1
from pydantic import TypeAdapter

from octomate.schemas.segments import (
    AtSegment,
    ImageSegment,
    ReplySegment,
    TextSegment,
)
from octomate.tentacles.channel.lark import LarkChromo
from octomate.types.json import JsonObject

JsonObjectAdapter = TypeAdapter(JsonObject)


def _loaded_json_object(value: str) -> JsonObject:
    return JsonObjectAdapter.validate_json(value)


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
