"""VercelEventStream: the Vercel channel's stream speaks octomate StreamEvents."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import cast
from uuid import uuid4

import anyio
from pydantic_ai.messages import (
    FunctionToolCallEvent,
    FunctionToolResultEvent,
    PartDeltaEvent,
    PartStartEvent,
    TextPart,
    TextPartDelta,
    ThinkingPart,
    ToolCallPart,
    ToolReturnPart,
)
from pydantic_ai.result import FinalResult
from pydantic_ai.ui import NativeEvent
from pydantic_ai.ui.vercel_ai.request_types import SubmitMessage
from pydantic_ai.ui.vercel_ai.response_types import (
    BaseChunk,
    ReasoningStartChunk,
    TextDeltaChunk,
    TextEndChunk,
    TextStartChunk,
    ToolInputAvailableChunk,
    ToolOutputAvailableChunk,
)

from octomate.capabilities.harness.events import (
    ActionBatchEvent,
    MessageSentEvent,
    ResultSegmentEvent,
    SubagentActivity,
    TodoCreatedEvent,
)
from octomate.capabilities.harness.react import ReactStreamEvent
from octomate.schemas.conversation import ChannelAddress
from octomate.schemas.deferred import DeferredQuestion
from octomate.schemas.segments import MarkdownSegment
from octomate.schemas.todos import Todo
from octomate.tentacles.agents.inkling.base import InklingOutput
from octomate.tentacles.channels.web.vercel.base import (
    VercelStreamItem,
    VercelTimelineFeeler,
    current_sink,
)
from octomate.tentacles.channels.web.vercel.event_stream import VercelEventStream
from tests.support.channels import RecordingApprovalFeeler, RecordingQuestionFeeler


async def _chunks(events: list[ReactStreamEvent[InklingOutput]]) -> list[BaseChunk]:
    async def gen() -> AsyncIterator[ReactStreamEvent[InklingOutput]]:
        for event in events:
            yield event

    stream = VercelEventStream(SubmitMessage(id="chat-1", messages=[]), sdk_version=6)
    return [
        chunk
        async for chunk in stream.transform_stream(
            # The same documented seam as the channel: pydantic-ai types the
            # stream over its native events; octomate events flow at runtime.
            cast(AsyncIterator[NativeEvent], gen())
        )
    ]


async def test_reply_deltas_stream_as_one_text_part() -> None:
    output: InklingOutput = "hello"
    chunks = await _chunks(
        [
            PartStartEvent(index=0, part=TextPart(content="hel")),
            PartDeltaEvent(index=0, delta=TextPartDelta(content_delta="lo")),
            FinalResult(output=output, tool_name=None, tool_call_id=None),
        ]
    )

    starts = [chunk for chunk in chunks if isinstance(chunk, TextStartChunk)]
    deltas = [chunk for chunk in chunks if isinstance(chunk, TextDeltaChunk)]
    assert len(starts) == 1
    assert [chunk.delta for chunk in deltas] == ["hel", "lo"]
    assert {chunk.id for chunk in deltas} == {starts[0].id}


async def test_final_result_closes_open_text_part() -> None:
    output: InklingOutput = "hello"
    chunks = await _chunks(
        [
            PartStartEvent(index=0, part=TextPart(content="hel")),
            PartDeltaEvent(index=0, delta=TextPartDelta(content_delta="lo")),
            FinalResult(output=output, tool_name=None, tool_call_id=None),
            ResultSegmentEvent(segment=MarkdownSegment(data={"text": "more"})),
        ]
    )

    starts = [chunk for chunk in chunks if isinstance(chunk, TextStartChunk)]
    deltas = [chunk for chunk in chunks if isinstance(chunk, TextDeltaChunk)]
    # Native text is handled by the stock Vercel stream; the segment after the
    # final result opens a fresh custom text part instead of joining with "\n\n".
    assert len(starts) == 2
    assert starts[1].id != starts[0].id
    assert [chunk.delta for chunk in deltas] == ["hel", "lo", "more"]


async def test_segments_join_the_reply_as_blocks() -> None:
    chunks = await _chunks(
        [
            ResultSegmentEvent(segment=MarkdownSegment(data={"text": "first"})),
            ResultSegmentEvent(segment=MarkdownSegment(data={"text": "second"})),
        ]
    )

    deltas = [chunk.delta for chunk in chunks if isinstance(chunk, TextDeltaChunk)]
    assert deltas == ["first", "\n\nsecond"]


async def test_todo_events_surface_as_markdown_text() -> None:
    todo = Todo(conversation_id=uuid4(), ref="T1", content="Find the docs")

    chunks = await _chunks([TodoCreatedEvent(todo=todo)])

    deltas = [chunk.delta for chunk in chunks if isinstance(chunk, TextDeltaChunk)]
    assert len(deltas) == 1
    assert "todo_created" in deltas[0]
    assert "T1" in deltas[0]
    assert "Find the docs" in deltas[0]


async def test_action_batch_surfaces_as_markdown_text() -> None:
    question = DeferredQuestion(
        tool_name="ask_questions",
        tool_call_id="c1",
        args={"question": "Pick one?"},
    )

    chunks = await _chunks([ActionBatchEvent(batch_id="b1", questions=[question])])

    deltas = [chunk.delta for chunk in chunks if isinstance(chunk, TextDeltaChunk)]
    assert len(deltas) == 1
    assert "Pending input" in deltas[0]
    assert "Pick one?" in deltas[0]


async def test_message_sent_renders_segments_as_reply_text() -> None:
    chunks = await _chunks(
        [MessageSentEvent(segments=[MarkdownSegment(data={"text": "progress"})])]
    )

    deltas = [chunk.delta for chunk in chunks if isinstance(chunk, TextDeltaChunk)]
    assert deltas == ["progress"]


async def test_mid_run_notice_text_part_closes_before_native_reply() -> None:
    # A mid-run send followed by the native reply: the notice's text part must be
    # closed before the reply's text part opens, or the two interleave into one
    # unbounded part and crash the dev UI.
    chunks = await _chunks(
        [
            MessageSentEvent(segments=[MarkdownSegment(data={"text": "progress"})]),
            PartStartEvent(index=0, part=TextPart(content="all set")),
            FinalResult(output="all set", tool_name=None, tool_call_id=None),
        ]
    )

    boundaries = [
        chunk.type
        for chunk in chunks
        if isinstance(chunk, (TextStartChunk, TextEndChunk))
    ]
    # The notice part is fully bounded before the reply part opens (no interleave).
    assert boundaries == ["text-start", "text-end", "text-start"]
    starts = [chunk for chunk in chunks if isinstance(chunk, TextStartChunk)]
    ends = [chunk for chunk in chunks if isinstance(chunk, TextEndChunk)]
    assert ends[0].id == starts[0].id  # the notice closed its own part
    assert starts[1].id != starts[0].id  # the reply opened a fresh part


async def test_native_events_pass_through_to_stock_handlers() -> None:
    chunks = await _chunks([PartStartEvent(index=0, part=ThinkingPart(content="hmm"))])

    assert any(isinstance(chunk, ReasoningStartChunk) for chunk in chunks)


async def test_parent_stream_hides_commission_tool_row() -> None:
    chunks = await _chunks(
        [
            FunctionToolCallEvent(
                ToolCallPart(
                    tool_name="commission",
                    args={"name": "audit"},
                    tool_call_id="call-a",
                )
            ),
            FunctionToolResultEvent(
                ToolReturnPart(
                    tool_name="commission",
                    content="report",
                    tool_call_id="call-a",
                )
            ),
        ]
    )

    assert not any(getattr(chunk, "tool_call_id", None) == "call-a" for chunk in chunks)


async def test_subagents_render_as_independent_standard_tool_parts() -> None:
    send, receive = anyio.create_memory_object_stream[VercelStreamItem](20)
    token = current_sink.set(send)
    feeler = VercelTimelineFeeler(
        ask_questions=RecordingQuestionFeeler(),
        approvals=RecordingApprovalFeeler(),
    )
    address = ChannelAddress(
        channel_tentacle_id="dev_ui",
        chat_type="private",
        chat_id="dev",
        user_id="dev",
        thread_id="chat-1",
    )
    try:
        async with feeler.open(address) as timeline:
            async with (
                timeline.open_subagent(
                    SubagentActivity("call-a", "commission", "audit")
                ) as first,
                timeline.open_subagent(
                    SubagentActivity("call-b", "commission", "tests")
                ) as second,
            ):
                await first.append_response("audit result")
                await second.append_response("test result")
                await first.settle("completed")
                await second.settle("failed", "one failure")
    finally:
        current_sink.reset(token)
        await send.aclose()

    items = [item async for item in receive]
    inputs = [item for item in items if isinstance(item, ToolInputAvailableChunk)]
    outputs = [item for item in items if isinstance(item, ToolOutputAvailableChunk)]
    assert [item.tool_call_id for item in inputs] == ["call-a", "call-b"]
    assert [item.tool_name for item in inputs] == ["commission", "commission"]
    first_outputs = [item for item in outputs if item.tool_call_id == "call-a"]
    second_outputs = [item for item in outputs if item.tool_call_id == "call-b"]
    assert first_outputs[-1].preliminary is False
    assert second_outputs[-1].preliminary is False
    assert "audit result" in str(first_outputs[-1].output)
    assert "test result" not in str(first_outputs[-1].output)
    assert "test result" in str(second_outputs[-1].output)
    assert "audit result" not in str(second_outputs[-1].output)
