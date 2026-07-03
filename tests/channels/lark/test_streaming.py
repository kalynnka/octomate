"""Lark streaming-card and consume (event-timeline) rendering tests."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from pathlib import Path
from uuid import uuid4

import pytest
from pydantic import TypeAdapter
from pydantic_ai import AgentRunResult, AgentRunResultEvent
from pydantic_ai.messages import (
    FunctionToolCallEvent,
    FunctionToolResultEvent,
    PartDeltaEvent,
    PartStartEvent,
    TextPart,
    ThinkingPart,
    ThinkingPartDelta,
    ToolCallPart,
    ToolReturnPart,
)

from octomate.capabilities.events import (
    ResultSegmentEvent,
    StreamEvents,
    TodoCompletedEvent,
    TodoCreatedEvent,
)
from octomate.schemas.conversation import ChannelAddress
from octomate.schemas.segments import CardData, CardSegment, ImageData, ImageSegment
from octomate.schemas.todos import Todo
from octomate.tentacles.channel.base import ChannelOutput
from octomate.tentacles.channel.lark.feelers import output as lark_output
from octomate.types.json import JsonObject
from tests.channels.lark.fakes import FakeLarkInk, lark_channel
from tests.support.channels import (
    RecordingDeferredActions,
    drive,
)
from tests.support.scenarios import action_batch, batch_actions, mid_run_notice, play

JsonObjectAdapter = TypeAdapter(JsonObject)


def _loaded_json_object(value: str) -> JsonObject:
    return JsonObjectAdapter.validate_json(value)


async def test_lark_consume_renders_timeline_per_event() -> None:
    ink = FakeLarkInk()
    channel = lark_channel(ink)
    address = ChannelAddress(
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

    message_id = await drive(channel, address, events())

    # Thinking + tool each posted as their own card (then folded via patch)...
    assert len(ink.created) == 2
    assert len(ink.patched) == 2
    # ...and the answer streamed into its own stream card, then finalized.
    assert ink.stream_cards
    assert any("done" in content for _card, content, _seq in ink.stream_updates)
    assert ink.finalized
    assert message_id == "stream-1"


async def test_lark_consume_renders_thinking_deltas_live(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(lark_output, "THINKING_FLUSH_INTERVAL", 0.0)
    ink = FakeLarkInk()
    channel = lark_channel(ink)
    address = ChannelAddress(
        channel_tentacle_id="lark",
        chat_type="private",
        chat_id="u1",
        user_id="u1",
    )

    async def events() -> AsyncIterator[
        StreamEvents[ChannelOutput] | AgentRunResultEvent[ChannelOutput]
    ]:
        yield PartStartEvent(index=0, part=ThinkingPart(content="checking"))
        yield PartDeltaEvent(index=0, delta=ThinkingPartDelta(content_delta=" the"))
        yield PartDeltaEvent(index=0, delta=ThinkingPartDelta(content_delta=" docs"))
        yield PartStartEvent(index=1, part=TextPart(content="done"))
        yield AgentRunResultEvent(AgentRunResult("done"))

    await drive(channel, address, events())

    # The thinking card patches live with the accumulating text ("Thinking…"),
    # then folds into a "Thought for Ns" collapsible panel with the full text
    # once the answer starts.
    live = [content for _id, content in ink.patched if "Thinking…" in content]
    assert ["checking" in content for content in live] == [True, True, True]
    assert "checking the docs" in live[-1]
    folded = [content for _id, content in ink.patched if "Thought for" in content]
    assert folded and "checking the docs" in folded[0]


async def test_lark_consume_renders_image_and_card_segments(
    tmp_path: Path,
) -> None:
    ink = FakeLarkInk()
    channel = lark_channel(ink)
    address = ChannelAddress(
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

    await drive(channel, address, events())

    # The image uploads then sends as an image message; the card posts as an
    # interactive message carrying the payload verbatim.
    assert ink.uploaded == [b"png-bytes"]
    image_contents = [
        content for _, _, msg_type, content in ink.created if msg_type == "image"
    ]
    assert [json.loads(content) for content in image_contents] == [
        {"image_key": "img-address-1"}
    ]
    interactive_contents = [
        content for _, _, msg_type, content in ink.created if msg_type == "interactive"
    ]
    assert json.loads(interactive_contents[-1]) == {"header": {"title": "t"}}


async def test_lark_card_segment_falls_back_to_raw_text_when_card_fails() -> None:
    ink = FakeLarkInk()
    ink.fail_interactive_send = True
    channel = lark_channel(ink)
    address = ChannelAddress(
        channel_tentacle_id="lark",
        chat_type="private",
        chat_id="u1",
        user_id="u1",
    )

    async def events() -> AsyncIterator[
        StreamEvents[ChannelOutput] | AgentRunResultEvent[ChannelOutput]
    ]:
        yield ResultSegmentEvent(
            segment=CardSegment(data=CardData(payload={"header": {"title": "t"}}))
        )

    await drive(channel, address, events())

    assert len(ink.created) == 1
    _, _, msg_type, content = ink.created[0]
    assert msg_type == "text"
    text = json.loads(content)["text"]
    assert "couldn't render this as a Lark card" in text
    assert '"header":{"title":"t"}' in text


async def test_lark_consume_renders_todo_checklist_card() -> None:
    ink = FakeLarkInk()
    channel = lark_channel(ink)
    address = ChannelAddress(
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

    await drive(channel, address, events())

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
    deferred = RecordingDeferredActions()
    channel = lark_channel(ink, deferred_actions=deferred)
    address = ChannelAddress(
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

    await drive(channel, 
        address,
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


async def test_lark_timeline_opens_new_answer_card_after_mid_run_notice() -> None:
    ink = FakeLarkInk()
    channel = lark_channel(ink)
    address = ChannelAddress(
        channel_tentacle_id="lark",
        chat_type="private",
        chat_id="u1",
        user_id="u1",
    )

    await drive(channel, address, play(mid_run_notice()))

    # The notice streamed into a first answer card which the rotation
    # finalized; the final answer opened a fresh card below the new activity.
    assert len(ink.stream_cards) == 2
    assert len(ink.finalized) == 2
    notice_card, answer_card = (card for card, _seq in ink.finalized)
    notice_text = "".join(
        content for card, content, _seq in ink.stream_updates if card is notice_card
    )
    answer_text = "".join(
        content for card, content, _seq in ink.stream_updates if card is answer_card
    )
    assert "trying another way" in notice_text
    assert "pinning the dependency" in answer_text
    # Both rounds' thinking and tool cards posted (and folded via patch).
    assert len(ink.created) == 4
    assert len(ink.patched) == 4


async def test_lark_answer_stream_preserves_markdown_tables_as_text() -> None:
    ink = FakeLarkInk()
    channel = lark_channel(ink)
    address = ChannelAddress(
        channel_tentacle_id="lark",
        chat_type="private",
        chat_id="u1",
        user_id="u1",
    )
    table = "| Name | Value |\n| --- | --- |\n| A | 1 |"

    async def events() -> AsyncIterator[
        StreamEvents[ChannelOutput] | AgentRunResultEvent[ChannelOutput]
    ]:
        yield PartStartEvent(index=0, part=TextPart(content=table))
        yield AgentRunResultEvent(AgentRunResult(table))

    await drive(channel, address, events())

    assert ink.stream_updates
    assert ink.stream_updates[-1][1] == f"```text\n{table}\n```"
