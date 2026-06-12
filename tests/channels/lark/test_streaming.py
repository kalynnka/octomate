"""Lark streaming-card and consume (event-timeline) rendering tests."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from pathlib import Path
from uuid import uuid4

from pydantic import TypeAdapter
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
from pydantic_ai.tools import DeferredToolRequests

from octomate.capabilities.events import (
    ResultSegmentEvent,
    StreamEvents,
    TodoCompletedEvent,
    TodoCreatedEvent,
)
from octomate.schemas.conversation import ConversationKey
from octomate.schemas.segments import CardData, CardSegment, ImageData, ImageSegment
from octomate.schemas.todos import Todo
from octomate.tentacles.channel.base import ChannelOutput
from octomate.tentacles.channel.lark.schema import LarkStreamCard
from octomate.types.json import JsonObject
from tests.channels.lark.fakes import FakeLarkInk, enable_lark_stream, lark_channel
from tests.support.channels import (
    FakeOctomate,
    RecordingDeferredActions,
    output_events,
    streamed_result,
)
from tests.support.scenarios import action_batch, batch_actions, play

JsonObjectAdapter = TypeAdapter(JsonObject)


def _loaded_json_object(value: str) -> JsonObject:
    return JsonObjectAdapter.validate_json(value)


async def test_lark_tentacle_streams_batched_card_updates_in_reply_thread() -> None:
    ink = FakeLarkInk()
    channel = lark_channel(ink)
    enable_lark_stream(channel, interval=0.2)
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
    channel = lark_channel(ink)
    enable_lark_stream(channel, interval=0.2)
    key = ConversationKey(
        channel_tentacle_id="lark",
        chat_type="group",
        chat_id="oc_group",
        user_id="ou_user",
        thread_id="om_parent",
    )

    await channel.feelers.markdown_stream.present_output(
        key,
        output_events("he", "llo"),
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
    channel = lark_channel(ink)
    enable_lark_stream(channel, interval=0)
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
    channel = lark_channel(ink)
    enable_lark_stream(channel, interval=0)
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
    channel = lark_channel(ink)
    enable_lark_stream(channel, interval=0)
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


async def test_lark_consume_renders_timeline_per_event() -> None:
    ink = FakeLarkInk()
    channel = lark_channel(ink)
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


async def test_lark_consume_renders_image_and_card_segments(
    tmp_path: Path,
) -> None:
    ink = FakeLarkInk()
    channel = lark_channel(ink)
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


async def test_lark_consume_renders_todo_checklist_card() -> None:
    ink = FakeLarkInk()
    channel = lark_channel(ink)
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


async def test_lark_consume_renders_action_batch_cards() -> None:
    ink = FakeLarkInk()
    channel = lark_channel(ink)
    channel.octomate = FakeOctomate()
    deferred = RecordingDeferredActions()
    channel.octomate.deferred_actions = deferred
    key = ConversationKey(
        channel_tentacle_id="lark",
        chat_type="private",
        chat_id="u1",
        user_id="u1",
    )
    # The lark question card serializes its batch id into the button state,
    # so the scripted actions need a real one.
    batch_id = uuid4()
    question, approval = batch_actions()
    question = question.model_copy(update={"batch_id": batch_id})
    approval = approval.model_copy(update={"batch_id": batch_id})

    await channel.consume(
        key,
        play(
            action_batch(
                batch_id=str(batch_id),
                questions=[question],
                approvals=[approval],
            )
        ),
    )

    # The question and approval each post as their own interactive card.
    assert [msg_type for _, _, msg_type, _ in ink.created] == [
        "interactive",
        "interactive",
    ]
    question_card = _loaded_json_object(ink.created[0][3])
    assert question_card["header"] == {
        "title": {"tag": "plain_text", "content": "Question"},
        "template": "blue",
    }
    assert "Which option should I take?" in ink.created[0][3]
    approval_card = _loaded_json_object(ink.created[1][3])
    assert approval_card["header"] == {
        "title": {"tag": "plain_text", "content": "Permission Required"},
        "template": "orange",
    }
    assert "deploy" in ink.created[1][3]
    # Both actions were marked presented with their card's message id.
    assert deferred.marked == [
        (question.id, "created-1"),
        (approval.id, "created-2"),
    ]
