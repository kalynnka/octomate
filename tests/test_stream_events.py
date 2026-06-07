"""UoW-1: the OctoStreamEvent types are well-formed and serialize round-trip.

Pure type-level tests — no agent run, no channels, no behaviour change.
"""

from __future__ import annotations

import pytest
from pydantic_ai.messages import PartStartEvent, TextPart

from octomate.schemas.deferred import ApprovalRequest
from octomate.schemas.segments import (
    ImageData,
    ImageSegment,
    MarkdownSegment,
    TextSegment,
)
from octomate.schemas.stream import (
    ActionRequestEvent,
    ApprovalRequestEvent,
    AskQuestionEvent,
    DisplayEvent,
    OctoStreamEvent,
    OctoStreamEventAdapter,
    OctoStreamEventListAdapter,
    SayChunkEvent,
    SayEndEvent,
    SayEvent,
    SayStartEvent,
    TodoItem,
    TodoListEvent,
)


def test_say_lifecycle_is_grouped_and_shares_message_id() -> None:
    start = SayStartEvent(message_id="m1")
    chunk = SayChunkEvent(
        message_id="m1", segments=[TextSegment(data={"text": "hi"})]
    )
    end = SayEndEvent(
        message_id="m1", segments=[TextSegment(data={"text": "hi there"})]
    )

    for ev in (start, chunk, end):
        assert isinstance(ev, SayEvent)
        assert isinstance(ev, DisplayEvent)
        assert ev.message_id == "m1"
    assert (start.event_kind, chunk.event_kind, end.event_kind) == (
        "say_start",
        "say_chunk",
        "say_end",
    )


def test_chunk_reuses_the_message_segment_vocabulary() -> None:
    chunk = SayChunkEvent(
        message_id="m1",
        segments=[
            MarkdownSegment(data={"text": "# Summary"}),
            ImageSegment(data=ImageData(file="/tmp/x.png")),
        ],
    )
    assert [seg.type for seg in chunk.segments] == ["markdown", "image"]


def test_todo_list_event_is_a_display_event() -> None:
    todo = TodoListEvent(items=[TodoItem(content="ship", status="in_progress")])
    assert isinstance(todo, DisplayEvent)
    assert todo.event_kind == "todo_list"
    assert todo.items[0].status == "in_progress"


def test_action_events_are_grouped_and_carry_correlation_ids() -> None:
    ask = AskQuestionEvent(
        action_id="a1",
        batch_id="b1",
        question={"question": "Proceed?", "choices": ["yes", "no"]},
    )
    approval = ApprovalRequestEvent(
        action_id="a2", batch_id="b1", approval=ApprovalRequest(tool_name="delete_repo")
    )

    for ev in (ask, approval):
        assert isinstance(ev, ActionRequestEvent)
        assert not isinstance(ev, DisplayEvent)
        assert ev.batch_id == "b1"
    assert (ask.event_kind, approval.event_kind) == ("ask_question", "approval_request")


def test_event_kinds_are_unique() -> None:
    kinds = [
        SayStartEvent(message_id="m").event_kind,
        SayChunkEvent(message_id="m", segments=[]).event_kind,
        SayEndEvent(message_id="m", segments=[]).event_kind,
        TodoListEvent(items=[]).event_kind,
        AskQuestionEvent(action_id="a", batch_id="b", question={"question": "?"}).event_kind,
        ApprovalRequestEvent(
            action_id="a", batch_id="b", approval=ApprovalRequest(tool_name="t")
        ).event_kind,
    ]
    assert len(kinds) == len(set(kinds))


@pytest.mark.parametrize(
    "event",
    [
        SayStartEvent(message_id="m1"),
        SayChunkEvent(message_id="m1", segments=[TextSegment(data={"text": "hi"})]),
        SayEndEvent(message_id="m1", segments=[TextSegment(data={"text": "hi"})]),
        TodoListEvent(items=[TodoItem(content="a")]),
        AskQuestionEvent(action_id="a", batch_id="b", question={"question": "?"}),
        ApprovalRequestEvent(
            action_id="a", batch_id="b", approval=ApprovalRequest(tool_name="t")
        ),
    ],
)
def test_adapter_roundtrips_octomate_events(event: OctoStreamEvent) -> None:
    restored = OctoStreamEventAdapter.validate_python(
        OctoStreamEventAdapter.dump_python(event)
    )
    assert type(restored) is type(event)


def test_adapter_passes_through_pydantic_ai_events() -> None:
    part = PartStartEvent(index=0, part=TextPart(content="hey"))
    restored = OctoStreamEventAdapter.validate_python(
        OctoStreamEventAdapter.dump_python(part)
    )
    assert isinstance(restored, PartStartEvent)


def test_list_adapter_roundtrips_a_mixed_stream() -> None:
    events: list[OctoStreamEvent] = [
        SayStartEvent(message_id="m1"),
        PartStartEvent(index=0, part=TextPart(content="hey")),
        AskQuestionEvent(action_id="a", batch_id="b", question={"question": "?"}),
    ]
    restored = OctoStreamEventListAdapter.validate_python(
        OctoStreamEventListAdapter.dump_python(events)
    )
    assert [type(x).__name__ for x in restored] == [
        "SayStartEvent",
        "PartStartEvent",
        "AskQuestionEvent",
    ]
