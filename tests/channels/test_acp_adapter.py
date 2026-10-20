from __future__ import annotations

import json
from typing import Literal

import pytest
from acp import schema
from pydantic import BaseModel, ConfigDict
from pydantic_ai.messages import (
    FunctionToolCallEvent,
    FunctionToolResultEvent,
    OutputToolCallEvent,
    OutputToolResultEvent,
    PartDeltaEvent,
    PartEndEvent,
    PartStartEvent,
    RetryPromptPart,
    TextPart,
    TextPartDelta,
    ThinkingPart,
    ThinkingPartDelta,
    ToolCallPart,
    ToolReturnPart,
)
from pydantic_ai.models.test import TestModel
from pydantic_ai.usage import RunUsage
from uuid_utils.compat import uuid7

from octomate.capabilities.harness.agent import Agent
from octomate.capabilities.harness.events import (
    ActionBatchEvent,
    ReflexAsleepEvent,
    ResultSegmentEvent,
    ResultTextDeltaEvent,
    RunResultEvent,
    RunStartedEvent,
    WireEvent,
    to_wire,
)
from octomate.schemas.conversation import ChannelAddress
from octomate.schemas.segments import (
    MarkdownSegment,
    MessageSegment,
    ReplySegment,
    TextSegment,
)
from octomate.tentacles.acp.adapter import AcpTranslator


@pytest.mark.parametrize("segments", [False, True])
async def test_harness_stream_reaches_acp_once(segments: bool) -> None:
    agent = (
        Agent(
            TestModel(custom_output_args=[{"type": "text", "data": {"text": "Hello"}}]),
            output_type=list[MessageSegment],
        )
        if segments
        else Agent(TestModel(custom_output_text="Hello"))
    )
    translator = AcpTranslator()
    received: list[str] = []

    async for event in agent.stream_events("Say hello"):
        wire = to_wire(event)
        if wire is None:
            continue
        for update in translator.translate(wire):
            assert isinstance(update, schema.AgentMessageChunk)
            assert isinstance(update.content, schema.TextContentBlock)
            received.append(update.content.text)

    assert "".join(received) == "Hello"


def test_text_and_thinking_stream_without_replaying_part_ends_or_final_output() -> None:
    translator = AcpTranslator()
    events: list[WireEvent] = [
        PartStartEvent(index=0, part=ThinkingPart(content="Consider")),
        PartDeltaEvent(index=0, delta=ThinkingPartDelta(content_delta=" this")),
        PartEndEvent(index=0, part=ThinkingPart(content="Consider this")),
        PartStartEvent(index=1, part=TextPart(content="Hello")),
        PartDeltaEvent(index=1, delta=TextPartDelta(content_delta=" world")),
        PartEndEvent(index=1, part=TextPart(content="Hello world")),
        RunResultEvent(output="Hello world", usage=RunUsage()),
    ]
    updates = [update for event in events for update in translator.translate(event)]

    assert [
        update.model_dump(mode="json", by_alias=True, exclude_none=True)
        for update in updates
    ] == [
        {
            "sessionUpdate": "agent_thought_chunk",
            "content": {"type": "text", "text": "Consider"},
        },
        {
            "sessionUpdate": "agent_thought_chunk",
            "content": {"type": "text", "text": " this"},
        },
        {
            "sessionUpdate": "agent_message_chunk",
            "content": {"type": "text", "text": "Hello"},
        },
        {
            "sessionUpdate": "agent_message_chunk",
            "content": {"type": "text", "text": " world"},
        },
    ]


def test_segment_output_streams_once_and_omits_reply_handles() -> None:
    translator = AcpTranslator()
    reply = ReplySegment(data={"id": str(uuid7())})
    heading = MarkdownSegment(data={"text": "**Answer**\n"})
    events: list[WireEvent] = [
        ResultSegmentEvent(segment=reply),
        ResultSegmentEvent(segment=heading),
        ResultTextDeltaEvent(delta="Hello "),
        ResultTextDeltaEvent(delta="world"),
        RunResultEvent(
            output=[reply, heading, TextSegment(data={"text": "Hello world"})],
            usage=RunUsage(),
        ),
    ]
    updates = [update for event in events for update in translator.translate(event)]

    assert len(updates) == 3
    assert [
        update.content.text
        for update in updates
        if isinstance(update, schema.AgentMessageChunk)
        and isinstance(update.content, schema.TextContentBlock)
    ] == ["**Answer**\n", "Hello ", "world"]


@pytest.mark.parametrize(
    "output",
    [
        "Final answer",
        [
            ReplySegment(data={"id": "reply"}),
            TextSegment(data={"text": "Final answer"}),
        ],
    ],
)
def test_nonstreaming_answer_after_thinking_and_empty_text_is_delivered_once(
    output: str | list[MessageSegment],
) -> None:
    translator = AcpTranslator()
    translator.translate(PartStartEvent(index=0, part=ThinkingPart(content="Thinking")))
    assert (
        translator.translate(PartStartEvent(index=1, part=TextPart(content=""))) == []
    )
    result = RunResultEvent(output=output, usage=RunUsage())

    updates = translator.translate(result)

    assert len(updates) == 1
    assert isinstance(updates[0], schema.AgentMessageChunk)
    assert isinstance(updates[0].content, schema.TextContentBlock)
    assert updates[0].content.text == "Final answer"
    assert translator.translate(result) == []


def test_run_start_resets_answer_tracking_but_preserves_announced_tool_ids() -> None:
    translator = AcpTranslator()
    call = FunctionToolCallEvent(
        part=ToolCallPart("lookup", {"query": "ACP"}, tool_call_id="call-1")
    )
    assert len(translator.translate(call)) == 1
    translator.translate(ResultTextDeltaEvent(delta="Earlier response"))

    assert (
        translator.translate(
            RunStartedEvent(
                address=ChannelAddress("acp", "thread", "user", "user", "session")
            )
        )
        == []
    )

    assert translator.translate(call) == []
    assert (
        len(
            translator.translate(
                RunResultEvent(output="Resumed answer", usage=RunUsage())
            )
        )
        == 1
    )


def test_tool_call_and_result_preserve_ids_and_structured_payloads() -> None:
    translator = AcpTranslator()
    part = ToolCallPart("lookup", {"query": "ACP"}, tool_call_id="call-1")
    assert translator.translate(PartStartEvent(index=0, part=part)) == []
    call = FunctionToolCallEvent(part=part)
    updates = translator.translate(call)

    assert len(updates) == 1
    update = updates[0]
    assert isinstance(update, schema.ToolCallStart)
    assert update.tool_call_id == "call-1"
    assert update.title == "lookup"
    assert update.status == "in_progress"
    assert update.raw_input == {"query": "ACP"}
    assert translator.translate(call) == []

    result = translator.translate(
        FunctionToolResultEvent(
            part=ToolReturnPart("lookup", {"hits": [1, 2]}, tool_call_id="call-1")
        )
    )[0]

    assert isinstance(result, schema.ToolCallProgress)
    assert result.tool_call_id == "call-1"
    assert result.status == "completed"
    assert result.raw_output == {"hits": [1, 2]}


@pytest.mark.parametrize("outcome", ["failed", "denied", "interrupted"])
def test_unsuccessful_tool_results_are_not_reported_as_completed(
    outcome: Literal["failed", "denied", "interrupted"],
) -> None:
    translator = AcpTranslator()
    result = translator.translate(
        FunctionToolResultEvent(
            part=ToolReturnPart(
                "lookup", "Could not finish", tool_call_id="call-1", outcome=outcome
            )
        )
    )[0]

    assert isinstance(result, schema.ToolCallProgress)
    assert result.status == "failed"
    assert result.raw_output == "Could not finish"


def test_retry_is_a_failed_tool_update_and_does_not_hide_the_next_answer() -> None:
    translator = AcpTranslator()
    translator.translate(ResultTextDeltaEvent(delta="Looking that up"))
    part = RetryPromptPart("Query required", tool_name="lookup", tool_call_id="call-1")

    result = translator.translate(FunctionToolResultEvent(part=part))[0]

    assert isinstance(result, schema.ToolCallProgress)
    assert result.status == "failed"
    assert result.raw_output == part.model_response()
    assert (
        len(
            translator.translate(
                RunResultEvent(output="Here is the answer", usage=RunUsage())
            )
        )
        == 1
    )


def test_output_tools_and_control_events_do_not_become_tool_activity_or_answers() -> (
    None
):
    translator = AcpTranslator()
    events: list[WireEvent] = [
        OutputToolCallEvent(
            part=ToolCallPart("final_result", {}, tool_call_id="output-1")
        ),
        OutputToolResultEvent(
            part=ToolReturnPart("final_result", "Answer", tool_call_id="output-1")
        ),
        ActionBatchEvent(batch_id=str(uuid7())),
        ReflexAsleepEvent(),
    ]

    assert [update for event in events for update in translator.translate(event)] == []
    assert (
        len(translator.translate(RunResultEvent(output="Answer", usage=RunUsage())))
        == 1
    )


@pytest.mark.parametrize("text", ["a" * 100_000, '你好🐙\n"\\' * 10_000])
@pytest.mark.parametrize("thought", [False, True])
def test_large_text_fits_serialized_acp_notifications_without_losing_content(
    text: str,
    thought: bool,
) -> None:
    translator = AcpTranslator()
    event = PartStartEvent(
        index=0, part=ThinkingPart(content=text) if thought else TextPart(content=text)
    )
    updates = translator.translate(event)
    received: list[str] = []
    session_id = str(uuid7())

    assert len(updates) > 1
    for update in updates:
        notification = schema.SessionNotification(session_id=session_id, update=update)
        wire = json.dumps(
            {
                "jsonrpc": "2.0",
                "method": "session/update",
                "params": notification.model_dump(
                    mode="json", by_alias=True, exclude_none=True
                ),
            }
        )
        assert len(wire.encode()) < 64 * 1024
        parsed = schema.SessionNotification.model_validate_json(
            notification.model_dump_json(by_alias=True)
        )
        assert isinstance(
            parsed.update, schema.AgentMessageChunk | schema.AgentThoughtChunk
        )
        assert isinstance(parsed.update.content, schema.TextContentBlock)
        received.append(parsed.update.content.text)
    assert "".join(received) == text


@pytest.mark.parametrize("call", [False, True])
def test_large_tool_payload_is_explicitly_bounded_without_mutating_the_event(
    call: bool,
) -> None:
    translator = AcpTranslator()
    payload = {"text": '🐙"\\' * 20_000}
    part = (
        ToolCallPart("lookup", payload, tool_call_id="call-1")
        if call
        else ToolReturnPart("lookup", payload, tool_call_id="call-1")
    )
    event = (
        FunctionToolCallEvent(part=part)
        if isinstance(part, ToolCallPart)
        else FunctionToolResultEvent(part=part)
    )
    update = translator.translate(event)[0]
    assert isinstance(update, schema.ToolCallStart | schema.ToolCallProgress)
    rendered = (
        update.raw_input
        if isinstance(update, schema.ToolCallStart)
        else update.raw_output
    )

    assert isinstance(rendered, dict)
    assert rendered["truncated"] is True
    assert rendered["original_length"] == len(json.dumps(payload))
    assert "preview" in rendered
    assert (part.args if isinstance(part, ToolCallPart) else part.content) == payload
    notification = schema.SessionNotification(session_id=str(uuid7()), update=update)
    assert (
        len(json.dumps(notification.model_dump(mode="json", by_alias=True)).encode())
        < 64 * 1024
    )


def test_tool_payload_serializes_models_and_binary_data() -> None:
    class Result(BaseModel):
        model_config = ConfigDict(ser_json_bytes="base64")

        data: bytes

    translator = AcpTranslator()
    update = translator.translate(
        FunctionToolResultEvent(
            part=ToolReturnPart(
                "lookup", Result(data=b"\x00\xff"), tool_call_id="call-1"
            )
        )
    )[0]

    assert isinstance(update, schema.ToolCallProgress)
    assert update.raw_output == {"data": "AP8="}

    binary_update = translator.translate(
        FunctionToolResultEvent(
            part=ToolReturnPart("lookup", b"\x00\xff", tool_call_id="call-2")
        )
    )[0]

    assert isinstance(binary_update, schema.ToolCallProgress)
    assert binary_update.raw_output == "AP8="
