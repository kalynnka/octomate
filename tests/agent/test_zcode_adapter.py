from typing import Literal

import pytest
from pydantic_ai.exceptions import AgentRunError
from pydantic_ai.messages import (
    FunctionToolCallEvent,
    FunctionToolResultEvent,
    ModelResponse,
    NativeToolCallPart,
    NativeToolReturnPart,
    PartDeltaEvent,
    PartStartEvent,
    TextPart,
    ThinkingPart,
)

from octomate.capabilities.harness.events import wire_event_adapter
from octomate.tentacles.zcode.adapter import ZcodeRunAccumulator
from octomate.tentacles.zcode.wire import SessionMessages, json_object_adapter
from tests.support.zcode import event, history_message


@pytest.mark.parametrize(
    ("kind", "first_chunk", "remaining"),
    [
        ("text_delta", "Here", "'s your triangle."),
        ("reasoning_delta", "The", " user asked for a triangle."),
    ],
)
def test_buffered_start_and_delta_render_each_chunk_once(
    kind: Literal["text_delta", "reasoning_delta"],
    first_chunk: str,
    remaining: str,
) -> None:
    accumulator = ZcodeRunAccumulator("prompt", input_id="input", model="GLM-5.3")
    list(
        accumulator.consume(
            event("turn.started", {"inputId": "input", "messageId": "prompt"})
        )
    )
    # Trunkline queues event objects before encoding them as SSE.
    start, delta = list(
        accumulator.consume(
            event(
                "model.streaming",
                {"kind": kind, "delta": first_chunk, "assistantMessageId": "answer"},
            )
        )
    )
    assert isinstance(start, PartStartEvent)
    assert isinstance(delta, PartDeltaEvent)
    start_payload = json_object_adapter.validate_json(
        wire_event_adapter.dump_json(start, warnings=False)
    )
    delta_payload = json_object_adapter.validate_json(
        wire_event_adapter.dump_json(delta, warnings=False)
    )
    part = start_payload["part"]
    change = delta_payload["delta"]
    assert isinstance(part, dict)
    assert isinstance(change, dict)
    initial = part["content"]
    addition = change["content_delta"]
    assert isinstance(initial, str)
    assert isinstance(addition, str)
    assert initial + addition == first_chunk

    list(
        accumulator.consume(
            event(
                "model.streaming",
                {"kind": kind, "delta": remaining, "assistantMessageId": "answer"},
            )
        )
    )
    assert isinstance(start.part, (TextPart, ThinkingPart))
    assert start.part.content == ""
    response = accumulator.messages[1]
    assert isinstance(response, ModelResponse)
    accumulated = response.parts[0]
    assert isinstance(accumulated, (TextPart, ThinkingPart))
    assert accumulated.content == first_chunk + remaining


def test_streams_reasoning_text_and_tools_without_duplicate_calls() -> None:
    accumulator = ZcodeRunAccumulator("prompt", input_id="input", model="GLM-5.3")
    frames = [
        event(
            "turn.started", {"inputId": "other", "messageId": "old"}, turn_id="old-turn"
        ),
        event(
            "model.streaming",
            {"kind": "text_delta", "delta": "old"},
            turn_id="old-turn",
        ),
        event("turn.started", {"inputId": "input", "messageId": "prompt"}),
        event(
            "model.streaming",
            {
                "kind": "reasoning_delta",
                "delta": "thinking",
                "assistantMessageId": "answer",
            },
        ),
        event(
            "model.streaming",
            {"kind": "text_delta", "delta": "reading", "assistantMessageId": "answer"},
        ),
        event(
            "model.streaming",
            {
                "kind": "tool_input_start",
                "toolName": "Read",
                "toolCallId": "read",
                "assistantMessageId": "answer",
            },
        ),
        event(
            "model.streaming",
            {"kind": "tool_input_delta", "toolCallId": "read", "delta": '{"path":"a"}'},
        ),
        event("model.streaming", {"kind": "tool_call", "toolCallId": "read"}),
        event(
            "tool.updated",
            {
                "kind": "scheduled",
                "toolCallId": "read",
                "toolName": "Read",
                "inputOmitted": True,
            },
        ),
        event(
            "tool.updated",
            {
                "kind": "result",
                "toolCallId": "read",
                "result": {"success": True, "output": "file text"},
            },
        ),
        event(
            "tool.updated",
            {
                "kind": "result",
                "toolCallId": "read",
                "result": {"success": True, "output": "file text"},
            },
        ),
        event(
            "turn.completed",
            {
                "response": "done",
                "resultType": "success",
                "usage": {
                    "modelRequestCount": 1,
                    "inputTokens": 20,
                    "outputTokens": 7,
                    "cacheReadTokens": 10,
                },
            },
        ),
    ]
    events = [item for frame in frames for item in accumulator.consume(frame)]
    calls = [item for item in events if isinstance(item, FunctionToolCallEvent)]
    returns = [item for item in events if isinstance(item, FunctionToolResultEvent)]
    assert len(calls) == len(returns) == 1
    assert calls[0].part.args == '{"path":"a"}'
    assert returns[0].part.content == "file text"
    assert len([item for item in events if isinstance(item, PartDeltaEvent)]) == 2
    response = accumulator.messages[1]
    assert isinstance(response, ModelResponse)
    assert [type(part) for part in response.parts] == [
        ThinkingPart,
        TextPart,
        NativeToolCallPart,
        NativeToolReturnPart,
    ]
    assert accumulator.result_text == "done"
    assert accumulator.usage.input_tokens == 20
    assert accumulator.usage.cache_read_tokens == 10


def test_reconciles_only_current_turn_and_requires_its_boundary() -> None:
    accumulator = ZcodeRunAccumulator("new", input_id="input", model="GLM-5.3")
    list(
        accumulator.consume(
            event("turn.started", {"inputId": "input", "messageId": "new"})
        )
    )
    delta = event("model.streaming", {"kind": "text_delta", "delta": "unfinished"})
    assert list(accumulator.consume(delta))
    assert not list(accumulator.consume(delta))
    history = SessionMessages.model_validate(
        {
            "messages": [
                history_message("old", "old answer"),
                history_message("new", "new", user=True),
                history_message("answer", "canonical"),
            ]
        }
    )
    accumulator.reconcile(history)
    assert len(accumulator.messages) == 2
    response = accumulator.messages[1]
    assert isinstance(response, ModelResponse)
    assert response.parts == [TextPart("canonical")]
    with pytest.raises(AgentRunError, match="current prompt"):
        accumulator.reconcile(SessionMessages(messages=[]))


def test_canonical_tool_errors_preserve_pairing() -> None:
    accumulator = ZcodeRunAccumulator("run", input_id="input", model="GLM-5.3")
    list(
        accumulator.consume(
            event("turn.started", {"inputId": "input", "messageId": "prompt"})
        )
    )
    answer = history_message("answer", "unused")
    answer["parts"] = [
        {
            "type": "tool",
            "callID": "t",
            "tool": "Bash",
            "state": {
                "status": "error",
                "input": {"command": "false"},
                "error": "denied",
            },
        }
    ]
    accumulator.reconcile(
        SessionMessages.model_validate(
            {"messages": [history_message("prompt", "run", user=True), answer]}
        )
    )
    response = accumulator.messages[1]
    assert isinstance(response, ModelResponse)
    call, result = response.parts
    assert isinstance(call, NativeToolCallPart)
    assert isinstance(result, NativeToolReturnPart)
    assert call.tool_call_id == result.tool_call_id == "t"
    assert result.outcome == "failed"
