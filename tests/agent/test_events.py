"""Catalog types: segment output events + display/action events.

The final output is Pydantic AI's own `FinalResult`, not a wrapper of ours, so it
has no catalog test here. Pure type-level tests — no agent run, no channels.
"""

from __future__ import annotations

from uuid_utils.compat import uuid7

from octomate.capabilities.harness.events import (
    ActionBatchEvent,
    DisplayEvent,
    ResultSegmentEvent,
    TodoCompletedEvent,
    TodoCreatedEvent,
    TodoDeletedEvent,
    TodoStatusChangedEvent,
    TodoUpdatedEvent,
)
from octomate.schemas.deferred import (
    ApprovalRequest,
    DeferredApproval,
    DeferredQuestion,
)
from octomate.schemas.segments import TextSegment
from octomate.schemas.todos import Todo


def _todo(**overrides: object) -> Todo:
    return Todo(
        conversation_id=uuid7(),
        ref="a1b2c3d4",
        content="ship it",
        active_form="shipping it",
        **overrides,  # pyright: ignore[reportArgumentType]
    )


def test_segment_event_carries_one_completed_segment() -> None:
    segment = TextSegment(data={"text": "hello"})
    event = ResultSegmentEvent(segment=segment)
    assert event.event_kind == "result_segment"
    assert event.segment is segment


def test_todo_events_are_display_events_carrying_the_todo() -> None:
    created = TodoCreatedEvent(todo=_todo(status="in_progress"))
    assert isinstance(created, DisplayEvent)
    assert created.event_kind == "todo_created"
    assert created.todo.status == "in_progress"

    changed = TodoStatusChangedEvent(
        todo=_todo(status="completed"), previous=_todo(status="in_progress")
    )
    assert changed.event_kind == "todo_status_changed"
    assert changed.previous is not None
    assert changed.previous.status == "in_progress"


def test_action_batch_event_carries_questions_and_approvals() -> None:
    event = ActionBatchEvent(
        batch_id="b1",
        questions=[
            DeferredQuestion(
                tool_name="ask_questions",
                tool_call_id="c1",
                args={"question": "Proceed?"},
            )
        ],
        approvals=[
            DeferredApproval(
                tool_name="delete_repo",
                tool_call_id="c2",
                args=ApprovalRequest(tool_name="delete_repo"),
            )
        ],
    )

    assert not isinstance(event, DisplayEvent)
    assert event.event_kind == "action_batch"
    assert event.batch_id == "b1"
    assert event.questions[0].args["question"] == "Proceed?"
    assert event.approvals[0].args.tool_name == "delete_repo"


def test_event_kinds_are_unique() -> None:
    kinds = [
        ResultSegmentEvent(segment=TextSegment(data={"text": "x"})).event_kind,
        TodoCreatedEvent(todo=_todo()).event_kind,
        TodoUpdatedEvent(todo=_todo()).event_kind,
        TodoStatusChangedEvent(todo=_todo()).event_kind,
        TodoCompletedEvent(todo=_todo()).event_kind,
        TodoDeletedEvent(todo=_todo()).event_kind,
        ActionBatchEvent(batch_id="b").event_kind,
    ]
    assert len(kinds) == len(set(kinds))
