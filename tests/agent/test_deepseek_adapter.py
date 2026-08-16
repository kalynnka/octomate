"""`DeepseekRunAccumulator` folds one driven turn's `session/event` frames into
pydantic-ai stream events plus the persisted message history. The scripts here
are the documented turn flow: turn/start → assistant/chunk* → assistant/message
→ tool/call → tool/result → turn/end."""

from __future__ import annotations

from pydantic_ai.messages import (
    FunctionToolCallEvent,
    FunctionToolResultEvent,
    ModelResponse,
    NativeToolCallPart,
    NativeToolReturnPart,
    PartDeltaEvent,
    PartEndEvent,
    PartStartEvent,
    TextPart,
    ThinkingPart,
    ToolReturnPart,
)

from octomate.capabilities.harness.events import StreamEvents
from octomate.tentacles.agents.deepseek.adapter import DeepseekRunAccumulator
from octomate.tentacles.agents.deepseek.wire import SessionEventFrame
from octomate.types.json import JsonValue


def frame(
    event_type: str, data: JsonValue = None, *, seq: int = 1
) -> SessionEventFrame:
    return SessionEventFrame.model_validate(
        {
            "type": "session/event",
            "sessionId": "sess-1",
            "event": {"type": event_type, "seq": seq, "time": 1.0, "data": data},
        }
    )


def chunk(kind: str, **fields: JsonValue) -> SessionEventFrame:
    return frame("assistant/chunk", {"chunk": {"type": kind, **fields}})


def message(text: str, usage: JsonValue = None) -> SessionEventFrame:
    data: dict[str, JsonValue] = {
        "message": {"content": [{"type": "text", "text": text}]}
    }
    if usage is not None:
        data["usage"] = usage
    return frame("assistant/message", data)


def turn_end(kind: str = "completed", error: JsonValue = None) -> SessionEventFrame:
    reason: dict[str, JsonValue] = {"kind": kind}
    if error is not None:
        reason["error"] = error
    return frame("turn/end", {"turn": 1, "reason": reason})


def consume_all(
    accumulator: DeepseekRunAccumulator, frames: list[SessionEventFrame]
) -> list[StreamEvents[str]]:
    events: list[StreamEvents[str]] = []
    for one in frames:
        events.extend(accumulator.consume(one))
    return events


def test_text_deltas_stream_and_the_commit_is_authoritative() -> None:
    accumulator = DeepseekRunAccumulator()
    accumulator.begin("prompt")

    events = consume_all(
        accumulator,
        [
            frame("turn/start", {"turn": 1}),
            chunk("text-delta", text="Hel"),
            chunk("text-delta", text="lo…"),
            message("Hello"),
            turn_end(),
        ],
    )

    kinds = [type(event).__name__ for event in events]
    assert kinds == [
        "PartStartEvent",
        "PartDeltaEvent",
        "PartDeltaEvent",
        "PartEndEvent",
    ]
    assert accumulator.result_text == "Hello"
    _request, response = accumulator.messages
    assert isinstance(response, ModelResponse)
    [part] = response.parts
    assert isinstance(part, TextPart)
    assert part.content == "Hello"
    assert response.finish_reason == "stop"
    assert response.provider_details is not None
    assert response.provider_details["turn_end"] == "completed"


def test_reasoning_then_text_commits_one_response_with_both_parts() -> None:
    accumulator = DeepseekRunAccumulator()

    events = consume_all(
        accumulator,
        [
            frame("turn/start", {"turn": 1}),
            chunk("reasoning-delta", text="thinking… "),
            chunk("reasoning-delta", text="still"),
            chunk("text-delta", text="answer"),
            message("answer"),
            turn_end(),
        ],
    )

    starts = [event for event in events if isinstance(event, PartStartEvent)]
    assert [type(event.part).__name__ for event in starts] == [
        "ThinkingPart",
        "TextPart",
    ]
    # The kind switch closes the thinking part before the text part opens.
    first_end = next(event for event in events if isinstance(event, PartEndEvent))
    assert isinstance(first_end.part, ThinkingPart)
    assert first_end.part.content == "thinking… still"
    [response] = accumulator.messages
    assert isinstance(response, ModelResponse)
    assert [type(part).__name__ for part in response.parts] == [
        "ThinkingPart",
        "TextPart",
    ]


def test_tool_call_and_result_pair_into_one_native_response() -> None:
    accumulator = DeepseekRunAccumulator()

    events = consume_all(
        accumulator,
        [
            frame("turn/start", {"turn": 1}),
            chunk("tool-call-delta", id="c1", name="bash", argumentsDelta='{"comm'),
            chunk("tool-call-delta", id="c1", argumentsDelta='and": "ls"}'),
            frame(
                "tool/call",
                {"callId": "c1", "name": "bash", "arguments": '{"command": "ls"}'},
            ),
            frame(
                "tool/result",
                {
                    "message": {
                        "content": [
                            {
                                "toolCallId": "c1",
                                "content": [{"type": "text", "text": "a.txt"}],
                                "isError": False,
                            }
                        ]
                    }
                },
            ),
            turn_end(),
        ],
    )

    [call_event] = [e for e in events if isinstance(e, FunctionToolCallEvent)]
    assert call_event.part.tool_name == "bash"
    [result_event] = [e for e in events if isinstance(e, FunctionToolResultEvent)]
    assert isinstance(result_event.part, ToolReturnPart)
    assert result_event.part.content == "a.txt"
    assert result_event.part.outcome == "success"
    [response] = accumulator.messages
    assert isinstance(response, ModelResponse)
    native_call, native_return = response.parts
    assert isinstance(native_call, NativeToolCallPart)
    assert native_call.args == {"command": "ls"}
    assert isinstance(native_return, NativeToolReturnPart)
    assert native_return.tool_call_id == "c1"


def test_a_result_without_a_call_synthesizes_the_pairing() -> None:
    accumulator = DeepseekRunAccumulator()

    events = consume_all(
        accumulator,
        [
            frame("turn/start", {"turn": 1}),
            frame(
                "tool/result",
                {
                    "message": {
                        "content": [
                            {
                                "toolCallId": "ghost",
                                "content": [{"type": "text", "text": "late"}],
                                "isError": True,
                            }
                        ]
                    }
                },
            ),
            turn_end(),
        ],
    )

    assert any(isinstance(event, FunctionToolCallEvent) for event in events)
    [result_event] = [e for e in events if isinstance(e, FunctionToolResultEvent)]
    assert isinstance(result_event.part, ToolReturnPart)
    assert result_event.part.outcome == "failed"
    [response] = accumulator.messages
    assert isinstance(response, ModelResponse)
    assert isinstance(response.parts[0], NativeToolCallPart)


def test_frames_before_turn_start_are_not_this_runs() -> None:
    accumulator = DeepseekRunAccumulator()

    events = consume_all(
        accumulator,
        [
            chunk("text-delta", text="stale tail"),
            message("stale answer"),
            turn_end(),
            frame("turn/start", {"turn": 2}),
            chunk("text-delta", text="fresh"),
            message("fresh"),
            turn_end(),
        ],
    )

    assert accumulator.result_text == "fresh"
    # The stale turn's frames produced no stream events and no messages.
    assert len([e for e in events if isinstance(e, PartStartEvent)]) == 1
    assert len(accumulator.messages) == 1


def test_unknown_event_types_ride_along_as_metadata() -> None:
    accumulator = DeepseekRunAccumulator()

    events = consume_all(
        accumulator,
        [
            frame("turn/start", {"turn": 1}),
            frame("plugin/esoteric", {"whatever": True}),
            message("done"),
            turn_end(),
        ],
    )

    assert not [e for e in events if isinstance(e, PartDeltaEvent)]
    [response] = accumulator.messages
    assert isinstance(response, ModelResponse)
    assert response.metadata is not None
    types = [
        event["type"]
        for event in response.metadata["events"]
        if isinstance(event, dict)
    ]
    assert "plugin/esoteric" in types


def test_message_usage_wins_over_chunk_usage_and_folds_into_the_run() -> None:
    accumulator = DeepseekRunAccumulator()

    consume_all(
        accumulator,
        [
            frame("turn/start", {"turn": 1}),
            chunk("text-delta", text="a"),
            chunk("usage", usage={"inputTokens": 1, "outputTokens": 1}),
            message(
                "a",
                usage={"inputTokens": 10, "outputTokens": 4, "reasoningTokens": 2},
            ),
            turn_end(),
        ],
    )

    assert accumulator.usage.input_tokens == 10
    assert accumulator.usage.output_tokens == 4
    assert accumulator.usage.requests == 1
    assert accumulator.usage.details["reasoning_tokens"] == 2


def test_chunk_usage_covers_a_message_that_carries_none() -> None:
    accumulator = DeepseekRunAccumulator()

    consume_all(
        accumulator,
        [
            frame("turn/start", {"turn": 1}),
            chunk("usage", usage={"inputTokens": 3, "outputTokens": 2}),
            chunk("text-delta", text="a"),
            message("a"),
            turn_end(),
        ],
    )

    assert accumulator.usage.input_tokens == 3
    assert accumulator.usage.requests == 1


def test_the_route_stamps_model_name_and_provider_details() -> None:
    accumulator = DeepseekRunAccumulator()

    consume_all(
        accumulator,
        [
            frame("turn/start", {"turn": 1}),
            frame(
                "request/context",
                {"provider": "deepseek-official", "model": "deepseek-v4-pro"},
            ),
            chunk("text-delta", text="a"),
            message("a"),
            turn_end(),
        ],
    )

    [response] = accumulator.messages
    assert isinstance(response, ModelResponse)
    assert response.model_name == "deepseek-v4-pro"
    assert response.provider_details is not None
    assert response.provider_details["provider"] == "deepseek-official"


def test_an_aborted_turn_commits_its_streamed_tail() -> None:
    accumulator = DeepseekRunAccumulator()

    events = consume_all(
        accumulator,
        [
            frame("turn/start", {"turn": 1}),
            chunk("text-delta", text="partial ans"),
            turn_end("aborted"),
        ],
    )

    assert any(isinstance(event, PartEndEvent) for event in events)
    assert accumulator.result_text == "partial ans"
    assert accumulator.turn_error is None
    [response] = accumulator.messages
    assert isinstance(response, ModelResponse)
    assert response.provider_details is not None
    assert response.provider_details["turn_end"] == "aborted"


def test_an_error_turn_records_the_failure() -> None:
    accumulator = DeepseekRunAccumulator()

    consume_all(
        accumulator,
        [
            frame("turn/start", {"turn": 1}),
            turn_end("error", error={"message": "rate limited", "code": "RATE_LIMIT"}),
        ],
    )

    assert accumulator.turn_ended
    assert accumulator.turn_error == "rate limited"
    # No content ever streamed, so the failure is the synthesized response.
    [response] = accumulator.messages
    assert isinstance(response, ModelResponse)
    assert response.finish_reason == "error"
    assert response.provider_details is not None
    assert response.provider_details["error"] == "rate limited"


def test_max_tokens_maps_to_the_length_finish_reason() -> None:
    accumulator = DeepseekRunAccumulator()

    consume_all(
        accumulator,
        [
            frame("turn/start", {"turn": 1}),
            chunk("text-delta", text="a"),
            message("a"),
            turn_end("max-tokens"),
        ],
    )

    [response] = accumulator.messages
    assert isinstance(response, ModelResponse)
    assert response.finish_reason == "length"
    assert accumulator.turn_error is None


def test_complete_command_is_a_whole_answer_without_a_turn() -> None:
    accumulator = DeepseekRunAccumulator()
    accumulator.begin("/permission workspace-write")

    events = list(accumulator.complete_command("preset workspace-write"))

    assert [type(event).__name__ for event in events] == [
        "PartStartEvent",
        "PartEndEvent",
    ]
    assert accumulator.turn_ended
    assert accumulator.result_text == "preset workspace-write"
    _request, response = accumulator.messages
    assert isinstance(response, ModelResponse)
    assert response.finish_reason == "stop"


def test_build_result_carries_history_usage_and_ids() -> None:
    accumulator = DeepseekRunAccumulator()
    accumulator.begin("prompt")
    consume_all(
        accumulator,
        [
            frame("turn/start", {"turn": 1}),
            message("done", usage={"inputTokens": 5, "outputTokens": 1}),
            turn_end(),
        ],
    )

    result = accumulator.build_result(run_id="run-1", conversation_id="conv-1")

    assert result.output == "done"
    assert result.all_messages() == accumulator.messages
    assert result.usage.input_tokens == 5
