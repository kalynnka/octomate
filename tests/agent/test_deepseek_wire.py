"""The hand-mirrored dsh wire contract stays permissive: known frames parse to
their variants, unknown frames and fields are carried rather than rejected, and
every event reader answers None instead of raising when the shape is not its.
The JSON literals here are the exact shapes dsh's own web client reads."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from octomate.tentacles.agents.deepseek.process import BANNER
from octomate.tentacles.agents.deepseek.wire import (
    ApprovalRequestedFrame,
    ClientRequest,
    ClientResponse,
    ErrResult,
    OkResult,
    QuestionRequestedFrame,
    ReasoningDeltaChunk,
    RpcError,
    ServerResponse,
    SessionEvent,
    SessionEventFrame,
    SessionSubscribedFrame,
    StreamErrorFrame,
    TextDeltaChunk,
    ToolCallDeltaChunk,
    UnknownFrame,
    UsageChunk,
    assistant_message_of,
    chunk_delta,
    history_entry_adapter,
    parse_mux_frame,
    permission_preset_of,
    provenance_of,
    request_route_of,
    text_of,
    tool_call_of,
    tool_result_of,
    turn_end_of,
    user_message_of,
)


def event(event_type: str, data: object = None, **extra: object) -> SessionEvent:
    return SessionEvent.model_validate(
        {"type": event_type, "seq": 1, "time": 1.0, "data": data, **extra}
    )


def test_parse_mux_frame_dispatches_every_known_frame() -> None:
    frames = [
        (
            {
                "type": "session/event",
                "sessionId": "s1",
                "event": {"type": "turn/start", "seq": 1, "time": 1.0, "data": None},
            },
            SessionEventFrame,
        ),
        (
            {"type": "session/subscribed", "sessionId": "s1", "lastSeq": 41},
            SessionSubscribedFrame,
        ),
        (
            {
                "type": "approval/requested",
                "sessionId": "s1",
                "approvalId": "a1",
                "toolName": "bash",
                "callId": "c1",
                "reason": "writes outside the workspace",
            },
            ApprovalRequestedFrame,
        ),
        (
            {
                "type": "question/requested",
                "sessionId": "s1",
                "questions": [{"id": "q1", "question": "Which branch?"}],
            },
            QuestionRequestedFrame,
        ),
        (
            {
                "type": "stream/error",
                "error": {"code": "internal", "message": "boom"},
            },
            StreamErrorFrame,
        ),
    ]
    for payload, expected in frames:
        assert isinstance(parse_mux_frame(payload), expected)


def test_an_unknown_frame_is_data_not_a_parse_failure() -> None:
    frame = parse_mux_frame(
        {"type": "session/projection", "sessionId": "s1", "key": "title", "seq": 7}
    )

    assert isinstance(frame, UnknownFrame)
    assert frame.session_id == "s1"
    assert frame.model_dump()["key"] == "title"


def test_a_known_frame_whose_shape_moved_degrades_to_unknown() -> None:
    # A future dsh renames lastSeq: the variant refuses, the frame still flows.
    frame = parse_mux_frame({"type": "session/subscribed", "sessionId": "s1"})

    assert isinstance(frame, UnknownFrame)


def test_a_payload_that_is_not_a_frame_at_all_raises() -> None:
    with pytest.raises(ValidationError):
        parse_mux_frame("not a frame")


def test_session_event_carries_unknown_types_and_fields() -> None:
    parsed = event(
        "plugin/custom-thing",
        {"anything": True},
        ignorable=True,
        surfaceOp="append",
    )

    assert parsed.type == "plugin/custom-thing"
    assert parsed.ignorable is True
    dump = parsed.model_dump(mode="json", by_alias=True)
    assert dump["surfaceOp"] == "append"


def test_client_messages_serialize_with_wire_names() -> None:
    request = ClientRequest(rpc_id="r1", method="session.prompt", payload={"a": 1})
    response = ClientResponse(
        rpc_id="r2", result=ErrResult(error=RpcError(code="cancelled", message="no"))
    )

    assert '"rpcId":"r1"' in request.model_dump_json(by_alias=True)
    assert '"type":"client-request"' in request.model_dump_json(by_alias=True)
    assert '"ok":false' in response.model_dump_json(by_alias=True)


def test_server_response_parses_both_result_branches() -> None:
    ok = ServerResponse.model_validate_json(
        '{"type": "server-response", "rpcId": "r1",'
        ' "result": {"ok": true, "value": {"accepted": true}}}'
    )
    err = ServerResponse.model_validate_json(
        '{"type": "server-response", "rpcId": "r1", "result":'
        ' {"ok": false, "error": {"code": "session-not-found", "message": "gone"}}}'
    )

    assert isinstance(ok.result, OkResult)
    assert ok.result.value == {"accepted": True}
    assert isinstance(err.result, ErrResult)
    assert err.result.error.code == "session-not-found"


def test_chunk_delta_narrows_the_four_rendered_kinds() -> None:
    text = chunk_delta(
        event("assistant/chunk", {"chunk": {"type": "text-delta", "text": "hi"}})
    )
    reasoning = chunk_delta(
        event("assistant/chunk", {"chunk": {"type": "reasoning-delta", "text": "hm"}})
    )
    tool = chunk_delta(
        event(
            "assistant/chunk",
            {
                "chunk": {
                    "type": "tool-call-delta",
                    "id": "c1",
                    "name": "bash",
                    "argumentsDelta": '{"comm',
                }
            },
        )
    )
    usage = chunk_delta(
        event(
            "assistant/chunk",
            {
                "chunk": {
                    "type": "usage",
                    "usage": {"inputTokens": 3, "outputTokens": 4},
                }
            },
        )
    )

    assert isinstance(text, TextDeltaChunk)
    assert text.text == "hi"
    assert isinstance(reasoning, ReasoningDeltaChunk)
    assert reasoning.text == "hm"
    assert isinstance(tool, ToolCallDeltaChunk)
    assert (tool.id, tool.name, tool.arguments_delta) == ("c1", "bash", '{"comm')
    assert isinstance(usage, UsageChunk)
    assert usage.usage.input_tokens == 3


def test_chunk_delta_returns_none_for_unknown_or_malformed_chunks() -> None:
    assert (
        chunk_delta(event("assistant/chunk", {"chunk": {"type": "audio-delta"}}))
        is None
    )
    assert chunk_delta(event("assistant/chunk", {"nope": 1})) is None
    assert chunk_delta(event("assistant/chunk", None)) is None


def test_assistant_message_reader_takes_text_usage_and_provenance() -> None:
    message = event(
        "assistant/message",
        {
            "message": {
                "content": [
                    {"type": "text", "text": "Hello "},
                    {
                        "type": "tool-call",
                        "id": "c1",
                        "name": "bash",
                        "arguments": "{}",
                    },
                    {"type": "text", "text": "world"},
                ],
                "source": {
                    "kind": "model",
                    "provider": "deepseek-official",
                    "model": "deepseek-v4-pro",
                },
            },
            "usage": {"inputTokens": 10, "outputTokens": 2, "reasoningTokens": 1},
        },
    )

    data = assistant_message_of(message)
    assert data is not None
    assert text_of(data.message.content) == "Hello world"
    assert data.usage is not None
    assert data.usage.reasoning_tokens == 1
    route = provenance_of(message)
    assert route is not None
    assert (route.provider, route.model) == ("deepseek-official", "deepseek-v4-pro")


def test_request_route_reader_names_the_step_model() -> None:
    route = request_route_of(
        event(
            "request/context",
            {"provider": "deepseek-official", "model": "deepseek-v4-flash"},
        )
    )

    assert route is not None
    assert route.model == "deepseek-v4-flash"
    assert request_route_of(event("request/context", {"provider": "x"})) is None


def test_tool_call_reader_keeps_arguments_raw() -> None:
    call = tool_call_of(
        event(
            "tool/call",
            {"callId": "c1", "name": "bash", "arguments": '{"command": "ls"}'},
        )
    )

    assert call is not None
    assert (call.call_id, call.name, call.arguments) == (
        "c1",
        "bash",
        '{"command": "ls"}',
    )
    assert tool_call_of(event("tool/call", {"name": "bash"})) is None


def test_tool_result_reader_reduces_the_first_block() -> None:
    result = tool_result_of(
        event(
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
        )
    )

    assert result is not None
    assert (result.call_id, result.text, result.is_error) == ("c1", "a.txt", False)


def test_tool_result_reader_reads_error_from_block_or_event() -> None:
    block_error = tool_result_of(
        event(
            "tool/result",
            {
                "message": {
                    "content": [{"toolCallId": "c1", "content": [], "isError": True}]
                }
            },
        )
    )
    event_error = tool_result_of(
        event(
            "tool/result",
            {
                "message": {"content": [{"toolCallId": "c1", "content": []}]},
                "error": {"message": "denied"},
            },
        )
    )

    assert block_error is not None
    assert block_error.is_error
    assert event_error is not None
    assert event_error.is_error
    assert tool_result_of(event("tool/result", {"message": {"content": []}})) is None


def test_turn_end_reader_names_the_reason_and_error() -> None:
    completed = turn_end_of(
        event("turn/end", {"turn": 1, "reason": {"kind": "completed"}})
    )
    errored = turn_end_of(
        event(
            "turn/end",
            {
                "turn": 1,
                "reason": {
                    "kind": "error",
                    "error": {"message": "rate limited", "code": "RATE_LIMIT"},
                },
            },
        )
    )

    assert completed is not None
    assert completed.reason is not None
    assert completed.reason.kind == "completed"
    assert errored is not None
    assert errored.reason is not None
    assert errored.reason.error_message == "rate limited"


def test_banner_regex_matches_the_readiness_line() -> None:
    match = BANNER.search("dsh web: http://127.0.0.1:51234")

    assert match is not None
    assert match.group(1) == "http://127.0.0.1:51234"
    assert BANNER.search("dsh loading plugins…") is None


def test_user_message_reader_carries_text_and_gateway_provenance() -> None:
    local = user_message_of(
        event(
            "user/message",
            {
                "role": "user",
                "content": [{"type": "text", "text": "hi"}],
                "source": {"kind": "user"},
            },
        )
    )
    via_gateway = user_message_of(
        event(
            "user/message",
            {
                "content": [{"type": "text", "text": "hello"}],
                "source": {"kind": "user", "rpcId": "rpc-9"},
            },
        )
    )

    assert local is not None
    assert text_of(local.content) == "hi"
    assert local.source.rpc_id is None
    assert via_gateway is not None
    assert via_gateway.source.rpc_id == "rpc-9"
    assert user_message_of(event("user/message", "not an object")) is None


def test_permission_preset_reader() -> None:
    assert (
        permission_preset_of(event("permission/preset", {"preset": "workspace-write"}))
        == "workspace-write"
    )
    assert permission_preset_of(event("permission/preset", {})) is None


def test_a_history_entry_parses_the_streamed_line_shape() -> None:
    entry = history_entry_adapter.validate_json(
        '{"event": {"type": "turn/start", "seq": 3, "time": 1.0, "data": {}},'
        ' "view": {"kind": "shell"}}'
    )
    bare = history_entry_adapter.validate_json(
        '{"event": {"type": "turn/end", "seq": 9, "time": 2.0, "data": null}}'
    )

    assert entry.event.seq == 3
    assert entry.view == {"kind": "shell"}
    assert bare.event.type == "turn/end"
    assert bare.view is None
