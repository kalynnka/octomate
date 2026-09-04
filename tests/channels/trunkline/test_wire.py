from __future__ import annotations

import json
import uuid

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

from octomate.capabilities.harness.events import (
    ActionBatchEvent,
    MessageSentEvent,
    OAuthDeviceAuthorizationEvent,
    ResultSegmentEvent,
    RunErrorEvent,
    SubagentSettledEvent,
    SubagentStartedEvent,
    TodoCreatedEvent,
    WireEvent,
    wire_event_adapter,
)
from octomate.schemas.deferred import (
    ApprovalRequest,
    DeferredApproval,
    DeferredQuestion,
)
from octomate.schemas.segments import TextSegment
from octomate.schemas.todos import Todo


def decode(event: WireEvent) -> dict[str, object]:
    """Round the event through the harness wire adapter back to its JSON payload."""
    payload = json.loads(wire_event_adapter.dump_json(event, warnings=False))
    assert isinstance(payload, dict)
    return payload


def test_native_events_serialize_with_their_own_discriminators() -> None:
    start = decode(PartStartEvent(index=0, part=TextPart(content="Hi")))
    assert start["event_kind"] == "part_start"
    part = start["part"]
    assert isinstance(part, dict)
    assert part["part_kind"] == "text"
    assert part["content"] == "Hi"

    thinking = decode(PartStartEvent(index=0, part=ThinkingPart(content="hmm")))
    part = thinking["part"]
    assert isinstance(part, dict)
    assert part["part_kind"] == "thinking"

    delta = decode(PartDeltaEvent(index=0, delta=ThinkingPartDelta(content_delta="…")))
    inner = delta["delta"]
    assert isinstance(inner, dict)
    assert inner["part_delta_kind"] == "thinking"

    call = decode(
        FunctionToolCallEvent(
            part=ToolCallPart(tool_name="search", args={"q": "x"}, tool_call_id="t1")
        )
    )
    assert call["event_kind"] == "function_tool_call"

    result = decode(
        FunctionToolResultEvent(
            part=ToolReturnPart(
                tool_name="search", content={"hits": 3}, tool_call_id="t1"
            )
        )
    )
    assert result["event_kind"] == "function_tool_result"
    part = result["part"]
    assert isinstance(part, dict)
    assert part["content"] == {"hits": 3}


def test_extension_events_share_the_event_kind_style() -> None:
    segment = decode(ResultSegmentEvent(segment=TextSegment(data={"text": "hi"})))
    assert segment["event_kind"] == "result_segment"

    todo = decode(
        TodoCreatedEvent(
            todo=Todo(
                conversation_id=uuid.UUID(int=0),
                ref="T1",
                content="write tests",
                position=1,
            )
        )
    )
    assert todo["event_kind"] == "todo_created"

    sent = decode(MessageSentEvent(segments=[TextSegment(data={"text": "fyi"})]))
    assert sent["event_kind"] == "message_sent"

    oauth = decode(
        OAuthDeviceAuthorizationEvent(
            connector_id="github",
            label="GitHub",
            authorization_uri="https://example.test/device",
            user_code="ABCD-1234",
        )
    )
    assert oauth["event_kind"] == "oauth_device_authorization"
    assert oauth["user_code"] == "ABCD-1234"


def test_action_batch_event_carries_persisted_actions() -> None:
    batch = decode(
        ActionBatchEvent(
            batch_id="b-1",
            questions=[
                DeferredQuestion(
                    kind="question",
                    tool_name="ask",
                    tool_call_id="t1",
                    args={"question": "Deploy where?", "choices": ["stg", "prod"]},
                )
            ],
            approvals=[
                DeferredApproval(
                    kind="approval",
                    tool_name="deploy",
                    tool_call_id="t2",
                    args=ApprovalRequest(tool_name="deploy", title="Deploy?"),
                )
            ],
        )
    )
    assert batch["event_kind"] == "action_batch"
    questions = batch["questions"]
    assert isinstance(questions, list)
    assert questions[0]["args"]["question"] == "Deploy where?"
    approvals = batch["approvals"]
    assert isinstance(approvals, list)
    assert approvals[0]["args"]["title"] == "Deploy?"


def test_transport_events() -> None:
    started = decode(
        SubagentStartedEvent(invocation_id="i1", kind="commission", name="probe")
    )
    assert started["event_kind"] == "subagent_started"

    settled = decode(
        SubagentSettledEvent(invocation_id="i1", status="completed", response="ok")
    )
    assert settled["event_kind"] == "subagent_settled"
    assert settled["response"] == "ok"

    error = decode(RunErrorEvent(message="boom"))
    assert error["event_kind"] == "run_error"
