"""The sidebar's projection retains status semantics and excludes run content."""

import pytest

from octomate.capabilities.harness.events import wire_event_adapter
from octomate.tentacles.trunkline.activity import ActivityUpdate
from octomate.types.json import JsonObject


@pytest.mark.parametrize(
    ("event", "expected"),
    [
        ({"event_kind": "reflex_awake"}, "preparing"),
        ({"event_kind": "reflex_phase_changed", "phase": "routing"}, "routing"),
        ({"event_kind": "reflex_phase_changed", "phase": "moving"}, "moving"),
        ({"event_kind": "reflex_phase_changed", "phase": "finalizing"}, "finalizing"),
        (
            {"event_kind": "run_activity_changed", "activity": "compacting"},
            "compacting",
        ),
        (
            {
                "event_kind": "part_start",
                "index": 0,
                "part": {"part_kind": "thinking", "content": "private thoughts"},
            },
            "thinking",
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
        ),
        (
            {
                "event_kind": "part_start",
                "index": 0,
                "part": {"part_kind": "text", "content": "private reply"},
            },
            "writing",
        ),
        (
            {
                "event_kind": "part_delta",
                "index": 0,
                "delta": {"part_delta_kind": "text", "content_delta": "private reply"},
            },
            "writing",
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
            "working",
        ),
        (
            {
                "event_kind": "function_tool_call",
                "part": {
                    "part_kind": "tool-call",
                    "tool_name": "shell",
                    "tool_call_id": "t",
                    "args": "private arguments",
                },
            },
            "using_tools",
        ),
        (
            {
                "event_kind": "output_tool_call",
                "part": {
                    "part_kind": "tool-call",
                    "tool_name": "shell",
                    "tool_call_id": "t",
                    "args": "private arguments",
                },
            },
            "using_tools",
        ),
        ({"event_kind": "run_ended", "outcome": "deferred"}, "run_ended"),
        ({"event_kind": "reflex_asleep", "outcome": "handed_off"}, "run_ended"),
        (
            {
                "event_kind": "run_error",
                "message": "private error",
                "trace_id": "private trace",
            },
            "run_ended",
        ),
    ],
)
def test_activity_projection(event: JsonObject, expected: str) -> None:
    update = ActivityUpdate.from_event(
        "id:thread", wire_event_adapter.validate_python(event)
    )
    assert update is not None
    data = update.event.model_dump()
    assert (data.get("activity") or data.get("phase") or data["event_kind"]) == expected
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
    )
    assert update is not None
    assert update.attention is True
    assert update.event.model_dump()["display_text"] == (
        "Waiting for input" if blocks_run else "Working · Input requested"
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
    update = ActivityUpdate.from_event("id:thread", event)
    assert update is not None
    assert update.event.model_dump()["display_text"] == "🐙" * 120
