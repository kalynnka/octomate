"""Catalog types: OutputDelta (generic) + display/action events.

The final output is Pydantic AI's own `FinalResult`, not a wrapper of ours, so it
has no catalog test here. Pure type-level tests — no agent run, no channels.
"""

from __future__ import annotations

from octomate.schemas.deferred import ApprovalRequest
from octomate.capabilities.events import (
    ActionRequestEvent,
    ApprovalRequestEvent,
    AskQuestionEvent,
    DisplayEvent,
    OutputDeltaEvent,
    TodoItem,
    TodoListEvent,
)


def test_output_delta_carries_the_typed_output() -> None:
    event = OutputDeltaEvent(output=[1, 2])
    assert event.event_kind == "output_delta"
    assert event.output == [1, 2]


def test_todo_list_is_a_display_event() -> None:
    todo = TodoListEvent(items=[TodoItem(content="ship", status="in_progress")])
    assert isinstance(todo, DisplayEvent)
    assert todo.event_kind == "todo_list"
    assert todo.items[0].status == "in_progress"


def test_action_events_are_grouped_and_carry_correlation_ids() -> None:
    ask = AskQuestionEvent(
        action_id="a1", batch_id="b1", question={"question": "Proceed?"}
    )
    approval = ApprovalRequestEvent(
        action_id="a2", batch_id="b1", approval=ApprovalRequest(tool_name="delete_repo")
    )

    for event in (ask, approval):
        assert isinstance(event, ActionRequestEvent)
        assert not isinstance(event, DisplayEvent)
        assert event.batch_id == "b1"
    assert (ask.event_kind, approval.event_kind) == ("ask_question", "approval_request")


def test_event_kinds_are_unique() -> None:
    kinds = [
        OutputDeltaEvent(output=None).event_kind,
        TodoListEvent(items=[]).event_kind,
        AskQuestionEvent(action_id="a", batch_id="b", question={"question": "?"}).event_kind,
        ApprovalRequestEvent(
            action_id="a", batch_id="b", approval=ApprovalRequest(tool_name="t")
        ).event_kind,
    ]
    assert len(kinds) == len(set(kinds))
