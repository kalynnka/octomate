"""The sidebar's projection retains status semantics and excludes run content."""

from datetime import UTC, datetime

import pytest

from octomate.events import (
    ReflexAsleepEvent,
    RunActivityChangedEvent,
    wire_event_adapter,
)
from octomate.tentacles.feelers.status import StatusNarrator
from octomate.tentacles.trunkline.activity import ActivityUpdate
from octomate.types.json import JsonObject


@pytest.mark.parametrize(
    ("event", "expected", "line"),
    [
        ({"event_kind": "reflex_awake"}, "working", "Waking"),
        (
            {"event_kind": "reflex_phase_changed", "phase": "routing"},
            "working",
            "Finding the right agent",
        ),
        (
            {"event_kind": "reflex_phase_changed", "phase": "moving"},
            "working",
            "Moving",
        ),
        (
            {
                "event_kind": "reflex_phase_changed",
                "phase": "moving",
                "spell": "summon",
            },
            "working",
            "Summoning",
        ),
        (
            {"event_kind": "reflex_phase_changed", "phase": "finalizing"},
            "working",
            "Wrapping up",
        ),
        (
            {
                "event_kind": "run_started",
                "address": {
                    "channel_tentacle_id": "trunkline",
                    "chat_type": "thread",
                    "chat_id": "owner",
                    "user_id": "owner",
                },
                "agent_tentacle_id": "claude",
            },
            "working",
            "Handing to Claude",
        ),
        (
            {"event_kind": "run_activity_changed", "activity": "compacting"},
            "compacting",
            "Compacting",
        ),
        (
            {
                "event_kind": "part_start",
                "index": 0,
                "part": {"part_kind": "thinking", "content": "private thoughts"},
            },
            "thinking",
            "Thinking",
        ),
        (
            {
                "event_kind": "part_delta",
                "index": 0,
                "delta": {
                    "part_delta_kind": "thinking",
                    "content_delta": "private thoughts",
                },
            },
            "thinking",
            "Thinking",
        ),
        (
            {
                "event_kind": "part_start",
                "index": 0,
                "part": {"part_kind": "text", "content": "private reply"},
            },
            "writing",
            "Writing",
        ),
        (
            {
                "event_kind": "part_delta",
                "index": 0,
                "delta": {"part_delta_kind": "text", "content_delta": "private reply"},
            },
            "writing",
            "Writing",
        ),
        (
            {
                "event_kind": "part_start",
                "index": 0,
                "part": {
                    "part_kind": "tool-call",
                    "tool_name": "shell",
                    "tool_call_id": "t",
                    "args": "private arguments",
                },
            },
            "using_tools",
            "Running a command",
        ),
        (
            {
                "event_kind": "function_tool_call",
                "part": {
                    "part_kind": "tool-call",
                    "tool_name": "mcp__linear__get_issue",
                    "tool_call_id": "t",
                    "args": "private arguments",
                },
            },
            "using_tools",
            "Get issue via Linear",
        ),
        (
            {
                "event_kind": "subagent_started",
                "invocation_id": "child",
                "kind": "commission",
                "name": "Scout",
            },
            "working",
            "Commissioning Scout",
        ),
        (
            {
                "event_kind": "output_tool_call",
                "part": {
                    "part_kind": "tool-call",
                    "tool_name": "final_result",
                    "tool_call_id": "t",
                    "args": "private arguments",
                },
            },
            "writing",
            "Writing",
        ),
        ({"event_kind": "run_ended", "outcome": "deferred"}, "run_ended", None),
        ({"event_kind": "reflex_asleep", "outcome": "handed_off"}, "run_ended", None),
        (
            {
                "event_kind": "run_error",
                "message": "private error",
                "trace_id": "private trace",
            },
            "run_ended",
            None,
        ),
    ],
)
def test_activity_projection(
    event: JsonObject, expected: str, line: str | None
) -> None:
    update = ActivityUpdate.from_event(
        "id:thread", wire_event_adapter.validate_python(event), StatusNarrator()
    )
    assert update is not None
    data = update.event.model_dump()
    assert (data.get("activity") or data["event_kind"]) == expected
    assert data.get("display_text") == line
    assert "private" not in update.model_dump_json()
    assert update.attention is False


@pytest.mark.parametrize("blocks_run", [True, False, None])
def test_activity_projection_keeps_attention_without_question_content(
    blocks_run: bool | None,
) -> None:
    update = ActivityUpdate.from_event(
        "key:thread",
        wire_event_adapter.validate_python(
            {
                "event_kind": "action_batch",
                "batch_id": "batch",
                "blocks_run": blocks_run,
                "questions": [],
                "approvals": [],
            }
        ),
        StatusNarrator(),
    )
    assert update is not None
    assert update.attention is True
    assert update.event.model_dump()["display_text"] == (
        "Waiting for your input" if blocks_run else "Working · Input requested"
    )
    assert "batch" not in update.model_dump_json()


def test_activity_projection_bounds_harness_labels() -> None:
    event = wire_event_adapter.validate_python(
        {
            "event_kind": "run_activity_changed",
            "activity": "thinking",
            "display_text": "\u0000\u202e \n " + "🐙" * 121,
        }
    )
    update = ActivityUpdate.from_event("id:thread", event, StatusNarrator())
    assert update is not None
    assert update.event.model_dump()["display_text"] == "🐙" * 120


def test_activity_projection_times_a_kick_only_while_it_runs() -> None:
    narrator = StatusNarrator()
    woke = datetime(2026, 11, 14, 9, 0, tzinfo=UTC)
    thinking = ActivityUpdate.from_event(
        "id:thread", RunActivityChangedEvent(activity="thinking"), narrator, woke
    )
    asleep = ActivityUpdate.from_event(
        "id:thread", ReflexAsleepEvent(outcome="completed"), narrator, woke
    )
    assert thinking is not None
    assert thinking.started_at == woke
    assert asleep is not None
    assert asleep.started_at is None
