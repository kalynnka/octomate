"""Status facts extend the existing reflex, run, and action event families."""

import pytest
from pydantic import BaseModel, JsonValue, ValidationError
from pydantic_ai.tools import DeferredToolRequests
from uuid_utils.compat import uuid7

from octomate.events import (
    ActionBatchEvent,
    ReflexAsleepEvent,
    ReflexPhaseChangedEvent,
    RunActivityChangedEvent,
    RunEndedEvent,
    to_wire,
    wire_event_adapter,
)
from octomate.schemas.conversation import ChannelAddress
from octomate.schemas.deferred import DeferredActionBatch, DeferredQuestion
from octomate.types.deferred import DeferredResponseMode


@pytest.mark.parametrize(
    "event",
    [
        ReflexPhaseChangedEvent(phase="preparing"),
        ReflexPhaseChangedEvent(phase="routing"),
        ReflexPhaseChangedEvent(phase="moving"),
        ReflexPhaseChangedEvent(phase="finalizing"),
        RunActivityChangedEvent(activity="working"),
        RunActivityChangedEvent(activity="thinking", display_text="Cogitating"),
        RunActivityChangedEvent(activity="using_tools"),
        RunActivityChangedEvent(activity="writing"),
        RunActivityChangedEvent(activity="compacting"),
    ],
)
def test_phase_and_activity_use_the_existing_wire(
    event: ReflexPhaseChangedEvent | RunActivityChangedEvent,
) -> None:
    restored = type(event).model_validate_json(
        wire_event_adapter.dump_json(event, warnings=False)
    )
    assert restored == event
    if isinstance(event, RunActivityChangedEvent):
        assert to_wire(event) is event


def test_standard_activity_survives_without_a_harness_label() -> None:
    event = RunActivityChangedEvent(activity="thinking")
    restored = RunActivityChangedEvent.model_validate_json(
        wire_event_adapter.dump_json(event, warnings=False)
    )
    assert restored.activity == "thinking"
    assert restored.display_text is None


@pytest.mark.parametrize(
    ("event_type", "outcome"),
    [
        (RunEndedEvent, "completed"),
        (RunEndedEvent, "failed"),
        (RunEndedEvent, "cancelled"),
        (RunEndedEvent, "deferred"),
        (ReflexAsleepEvent, "completed"),
        (ReflexAsleepEvent, "failed"),
        (ReflexAsleepEvent, "cancelled"),
        (ReflexAsleepEvent, "deferred"),
        (ReflexAsleepEvent, "handed_off"),
    ],
)
def test_end_events_preserve_their_own_outcome(
    event_type: type[RunEndedEvent] | type[ReflexAsleepEvent], outcome: str
) -> None:
    event = event_type.model_validate({"outcome": outcome})
    restored = event_type.model_validate_json(
        wire_event_adapter.dump_json(event, warnings=False)
    )
    assert restored.outcome == outcome


@pytest.mark.parametrize(
    ("response_mode", "blocks_run"),
    [("live", True), ("resume", True), ("resume", False)],
)
def test_action_batch_preserves_response_mode_and_explicit_blocking(
    response_mode: DeferredResponseMode, blocks_run: bool
) -> None:
    address = ChannelAddress(
        channel_tentacle_id="test", chat_type="dm", chat_id="chat", user_id="user"
    )
    question = DeferredQuestion(
        tool_name="ask_questions",
        tool_call_id="question-call",
        args={"question": "Which option?"},
    )
    # This schema is never persisted; the factory only reads its presentation data.
    batch = DeferredActionBatch.model_validate(
        {
            "conversation_id": uuid7(),
            "agent_tentacle_id": "inkling",
            "response_mode": response_mode,
            "source_address": address,
            "target_address": address,
            "requests": DeferredToolRequests(),
            "questions": [question],
            "approvals": [],
        }
    )
    event = ActionBatchEvent.from_batch(batch, blocks_run=blocks_run)
    restored = ActionBatchEvent.model_validate_json(
        wire_event_adapter.dump_json(event, warnings=False)
    )
    assert restored.batch_id == str(batch.id)
    assert restored.response_mode == response_mode
    assert restored.blocks_run is blocks_run
    assert restored.questions[0].id == question.id
    assert restored.questions[0].tool_call_id == "question-call"
    assert ActionBatchEvent.from_batch(batch).blocks_run is None


def test_async_question_and_thinking_remain_separate_facts() -> None:
    request = ActionBatchEvent(
        batch_id=str(uuid7()), response_mode="resume", blocks_run=False
    )
    activity = RunActivityChangedEvent(activity="thinking")
    assert request.response_mode == "resume"
    assert request.blocks_run is False
    assert activity.activity == "thinking"


def test_legacy_events_do_not_invent_outcomes_or_waits() -> None:
    assert RunEndedEvent.model_validate({"event_kind": "run_ended"}).outcome is None
    assert (
        ReflexAsleepEvent.model_validate({"event_kind": "reflex_asleep"}).outcome
        is None
    )
    batch = ActionBatchEvent.model_validate(
        {"event_kind": "action_batch", "batch_id": "old"}
    )
    assert batch.response_mode is None
    assert batch.blocks_run is None


@pytest.mark.parametrize(
    ("event_type", "payload"),
    [
        (ReflexPhaseChangedEvent, {"phase": "thinking"}),
        (RunActivityChangedEvent, {"activity": "routing"}),
        (RunActivityChangedEvent, {"activity": "thinking", "display_text": ""}),
        (RunEndedEvent, {"outcome": "handed_off"}),
        (ReflexAsleepEvent, {"outcome": "thinking"}),
        (ActionBatchEvent, {"batch_id": "b", "response_mode": "suspended"}),
    ],
)
def test_status_fields_reject_values_from_the_wrong_lifecycle(
    event_type: type[BaseModel], payload: dict[str, JsonValue]
) -> None:
    with pytest.raises(ValidationError):
        event_type.model_validate(payload)
