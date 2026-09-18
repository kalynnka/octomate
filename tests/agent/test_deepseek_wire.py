"""The hand-mirrored dsh wire contract stays permissive: known frames parse to
their variants, unknown frames and fields are carried rather than rejected, and
every event reader answers None instead of raising when the shape is not its.
The JSON literals here are the exact shapes dsh's own web client reads."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from octomate.tentacles.deepseek.process import BANNER
from octomate.tentacles.deepseek.wire import (
    ClientRequest,
    ErrResult,
    OkResult,
    ReasoningDeltaChunk,
    RemoteInvocation,
    RemoteItem,
    ServerResponse,
    SessionEvent,
    SessionRecord,
    TextDeltaChunk,
    ToolCallDeltaChunk,
    UsageChunk,
    assistant_message_of,
    chunk_delta,
    history_entry_adapter,
    permission_preset_of,
    provenance_of,
    remote_event_adapter,
    remote_message_adapter,
    request_route_of,
    session_follow_adapter,
    text_of,
    tool_call_of,
    tool_result_of,
    turn_end_of,
    user_message_of,
)
from octomate.types.json import JsonValue


def event(event_type: str, data: object = None, **extra: object) -> SessionEvent:
    return SessionEvent.model_validate(
        {"type": event_type, "seq": 1, "time": 1.0, "data": data, **extra}
    )


def test_remote_stream_parses_durable_events_and_waterfalls() -> None:
    item = remote_message_adapter.validate_python(
        {
            "type": "item",
            "streamId": "s1",
            "value": {
                "type": "event",
                "event": {
                    "type": "turn/start",
                    "seq": 1,
                    "time": 1,
                    "data": {"turn": 1},
                },
            },
        }
    )
    assert isinstance(item, RemoteItem)
    entry = session_follow_adapter.validate_python(item.value)
    assert isinstance(entry, SessionRecord)
    assert entry.event.seq == 1
    invocation = remote_event_adapter.validate_python(
        {
            "type": "waterfall",
            "event": "approval/request",
            "eventId": "event-1",
            "agentId": "s1",
            "request": {"toolName": "bash"},
        }
    )
    assert isinstance(invocation, RemoteInvocation)
    assert invocation.agent_id == "s1"


@pytest.mark.parametrize(
    "payload", ["not a frame", {"type": "item"}, {"type": "old-protocol"}]
)
def test_incompatible_transport_frames_fail_explicitly(payload: JsonValue) -> None:
    with pytest.raises(ValidationError):
        remote_message_adapter.validate_python(payload)


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
    request = ClientRequest(
        rpc_id="r1", method="session/prompt", payload={"args": {"request": {"a": 1}}}
    )
    assert '"rpcId":"r1"' in request.model_dump_json(by_alias=True)
    assert '"type":"client-request"' in request.model_dump_json(by_alias=True)


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
