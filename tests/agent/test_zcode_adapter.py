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


def test_native_timeline_before_prompt_does_not_invalidate_history_cursor() -> None:
    accumulator = ZcodeRunAccumulator("new", input_id="input", model="GLM-5.3")
    list(
        accumulator.consume(
            event("turn.started", {"inputId": "input", "messageId": "prompt"})
        )
    )
    timeline = history_message("timeline", "")
    info = timeline["info"]
    assert isinstance(info, dict)
    info["semantics"] = {
        "origin": "system",
        "kind": "timeline_event",
        "uiVisibility": "visible",
        "transcriptVisibility": "visible",
        "providerVisibility": "hidden",
    }
    timeline["parts"] = [{"type": "timeline"}]
    accumulator.reconcile(
        SessionMessages.model_validate(
            {
                "messages": [
                    timeline,
                    history_message("prompt", "new", user=True),
                    history_message("answer", "reply"),
                ],
            }
        ),
        after_message_id="previous-answer",
    )
    assert len(accumulator.messages) == 2
    response = accumulator.messages[1]
    assert isinstance(response, ModelResponse)
    assert response.parts == [TextPart("reply")]


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


def test_native_assistant_history_keeps_content_tools_and_usage() -> None:
    accumulator = ZcodeRunAccumulator("run", input_id="input", model="GLM-5.3")
    list(
        accumulator.consume(
            event("turn.started", {"inputId": "input", "messageId": "prompt"})
        )
    )
    answer = history_message("answer", "done")
    info = answer["info"]
    assert isinstance(info, dict)
    info.update(
        {
            "finish": "tool-calls",
            "tokens": {
                "input": 20,
                "output": 7,
                "reasoning": 3,
                "cache": {"read": 10, "write": 4},
            },
        }
    )
    answer["parts"] = [
        {"type": "reasoning", "text": "Inspect the workspace"},
        {"type": "text", "text": "Workspace read"},
        {"type": "text", "text": "synthetic context", "synthetic": True},
        {"type": "text", "text": "ignored context", "ignored": True},
        {
            "type": "tool",
            "callID": "read",
            "tool": "Bash",
            "state": {
                "status": "completed",
                "input": {"command": "pwd"},
                "output": "/workspace",
            },
        },
    ]
    accumulator.reconcile(
        SessionMessages.model_validate(
            {
                "messages": [history_message("prompt", "run", user=True), answer],
            }
        )
    )
    assert len(accumulator.messages) == 2
    response = accumulator.messages[1]
    assert isinstance(response, ModelResponse)
    assert response.parts[:2] == [
        ThinkingPart("Inspect the workspace", provider_name="zcode"),
        TextPart("Workspace read"),
    ]
    call, result = response.parts[2:]
    assert isinstance(call, NativeToolCallPart)
    assert isinstance(result, NativeToolReturnPart)
    assert call.tool_name == result.tool_name == "Bash"
    assert call.tool_call_id == result.tool_call_id == "read"
    assert call.args == {"command": "pwd"}
    assert result.content == "/workspace"
    assert result.outcome == "success"
    assert response.usage.input_tokens == 20
    assert response.usage.output_tokens == 7
    assert response.usage.cache_read_tokens == 10
    assert response.usage.cache_write_tokens == 4
    assert response.usage.details == {"reasoning_tokens": 3}
    assert response.finish_reason == "tool_call"


@pytest.mark.parametrize("hidden", ["synthetic", "visibility", "semantics", "part"])
def test_native_context_is_not_recorded_as_human_input(hidden: str) -> None:
    accumulator = ZcodeRunAccumulator("prompt", input_id="input", model="GLM-5.3")
    list(
        accumulator.consume(
            event("turn.started", {"inputId": "input", "messageId": "prompt"})
        )
    )
    reminder = history_message("reminder", "internal reminder", user=True)
    info = reminder["info"]
    assert isinstance(info, dict)
    if hidden == "synthetic":
        info["synthetic"] = True
    elif hidden == "visibility":
        info["visibility"] = "model-only"
    elif hidden == "semantics":
        info["semantics"] = {
            "origin": "agent_runtime",
            "kind": "system_reminder",
            "uiVisibility": "hidden",
            "transcriptVisibility": "hidden",
            "providerVisibility": "visible",
        }
    else:
        reminder["parts"] = [
            {"type": "text", "text": "internal reminder", "synthetic": True}
        ]
    accumulator.reconcile(
        SessionMessages.model_validate(
            {
                "messages": [
                    history_message("prompt", "prompt", user=True),
                    reminder,
                    history_message("answer", "answer"),
                ]
            }
        )
    )
    assert len(accumulator.messages) == 2
    assert "internal reminder" not in str(accumulator.messages)


@pytest.mark.parametrize(
    ("finish", "expected"),
    [
        ("length", "length"),
        ("tool-calls", "tool_call"),
        ("content-filter", "content_filter"),
        ("new-native-reason", None),
        (None, None),
    ],
)
def test_native_finish_reasons_are_not_reported_as_success(
    finish: str | None, expected: str | None
) -> None:
    accumulator = ZcodeRunAccumulator("prompt", input_id="input", model="GLM-5.3")
    list(
        accumulator.consume(
            event("turn.started", {"inputId": "input", "messageId": "prompt"})
        )
    )
    answer = history_message("answer", "partial")
    info = answer["info"]
    assert isinstance(info, dict)
    info["finish"] = finish
    accumulator.reconcile(
        SessionMessages.model_validate(
            {"messages": [history_message("prompt", "prompt", user=True), answer]}
        )
    )
    response = accumulator.messages[-1]
    assert isinstance(response, ModelResponse)
    assert response.finish_reason == expected
    assert response.provider_details == {"finish": finish}


def test_reconciliation_discards_abandoned_attempts_and_preserves_errors() -> None:
    accumulator = ZcodeRunAccumulator("prompt", input_id="input", model="GLM-5.3")
    list(
        accumulator.consume(
            event("turn.started", {"inputId": "input", "messageId": "prompt"})
        )
    )
    discarded = history_message("discarded", "abandoned")
    failed = history_message("failed", "incomplete")
    for message, finish, error in [
        (discarded, "stream_recovery_discarded", "StreamRecoveryDiscarded"),
        (failed, "stop", "APIError"),
    ]:
        info = message["info"]
        assert isinstance(info, dict)
        info.update(
            {
                "finish": finish,
                "error": {"name": error, "data": {"message": "native failure"}},
            }
        )
    accumulator.reconcile(
        SessionMessages.model_validate(
            {
                "messages": [
                    history_message("prompt", "prompt", user=True),
                    discarded,
                    failed,
                ]
            }
        )
    )
    assert len(accumulator.messages) == 2
    response = accumulator.messages[-1]
    assert isinstance(response, ModelResponse)
    assert response.parts == [TextPart("incomplete")]
    assert response.finish_reason == "error"
    assert response.provider_details is not None
    assert response.provider_details["error"]["name"] == "APIError"


@pytest.mark.parametrize("control", [True, False])
def test_completed_turn_without_stored_prompt_keeps_its_response(control: bool) -> None:
    accumulator = ZcodeRunAccumulator("prompt", input_id="input", model="GLM-5.3")
    list(
        accumulator.consume(
            event(
                "turn.started",
                {
                    "inputId": "input",
                    **({} if control else {"messageId": "blocked-prompt"}),
                },
            )
        )
    )
    list(
        accumulator.consume(
            event(
                "turn.completed",
                {"resultType": "success", "response": "completed without a prompt"},
            )
        )
    )
    accumulator.reconcile(SessionMessages(messages=[]))
    response = accumulator.messages[-1]
    assert isinstance(response, ModelResponse)
    assert response.parts == [TextPart("completed without a prompt")]


def test_compaction_without_prompt_preserves_model_usage_and_command_response() -> None:
    accumulator = ZcodeRunAccumulator("/compact", input_id="input", model="GLM-5.3")
    list(
        accumulator.consume(
            event("turn.started", {"inputId": "input", "inputVisibility": "model-only"})
        )
    )
    list(
        accumulator.consume(
            event(
                "model.streaming",
                {"kind": "text_delta", "delta": "internal compaction summary"},
            )
        )
    )
    list(
        accumulator.consume(
            event(
                "turn.completed",
                {
                    "resultType": "success",
                    "response": "Compacted conversation",
                    "usage": {"modelRequestCount": 1, "inputTokens": 100},
                },
            )
        )
    )
    accumulator.reconcile(SessionMessages(messages=[]))
    assert accumulator.usage.requests == 1
    assert accumulator.usage.input_tokens == 100
    assert len(accumulator.messages) == 2
    response = accumulator.messages[-1]
    assert isinstance(response, ModelResponse)
    assert response.parts == [TextPart("Compacted conversation")]
