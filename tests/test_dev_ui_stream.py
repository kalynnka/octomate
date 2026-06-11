"""OctomateUIEventStream: dev_ui's Vercel stream speaks the octomate StreamEvents."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import cast
from uuid import uuid4

from pydantic_ai.messages import PartStartEvent, ThinkingPart
from pydantic_ai.result import FinalResult
from pydantic_ai.ui import NativeEvent
from pydantic_ai.ui.vercel_ai.request_types import SubmitMessage
from pydantic_ai.ui.vercel_ai.response_types import (
    BaseChunk,
    DataChunk,
    ReasoningStartChunk,
    TextDeltaChunk,
    TextEndChunk,
    TextStartChunk,
)

from octomate.capabilities.events import (
    ActionBatchEvent,
    ResultSegmentEvent,
    ResultTextDeltaEvent,
    TodoCreatedEvent,
)
from octomate.capabilities.react import ReactStreamEvent
from octomate.schemas.deferred import DeferredQuestion
from octomate.schemas.segments import MarkdownSegment
from octomate.schemas.todos import Todo
from octomate.tentacles.agent.inkling.base import InklingOutput
from octomate.web.dev_ui.event_stream import OctomateUIEventStream


async def _chunks(events: list[ReactStreamEvent[InklingOutput]]) -> list[BaseChunk]:
    async def gen() -> AsyncIterator[ReactStreamEvent[InklingOutput]]:
        for event in events:
            yield event

    stream = OctomateUIEventStream(SubmitMessage(id="chat-1", messages=[]), sdk_version=6)
    return [
        chunk
        async for chunk in stream.transform_stream(
            # The same documented seam as the adapter: pydantic-ai types the
            # stream over its native events; octomate events flow at runtime.
            cast(AsyncIterator[NativeEvent], gen())
        )
    ]


async def test_reply_deltas_stream_as_one_text_part() -> None:
    output: InklingOutput = [MarkdownSegment(data={"text": "hello"})]
    chunks = await _chunks(
        [
            ResultTextDeltaEvent(delta="hel"),
            ResultTextDeltaEvent(delta="lo"),
            FinalResult(output=output, tool_name=None, tool_call_id=None),
        ]
    )

    starts = [chunk for chunk in chunks if isinstance(chunk, TextStartChunk)]
    deltas = [chunk for chunk in chunks if isinstance(chunk, TextDeltaChunk)]
    ends = [chunk for chunk in chunks if isinstance(chunk, TextEndChunk)]
    assert len(starts) == 1
    assert [chunk.delta for chunk in deltas] == ["hel", "lo"]
    assert len(ends) == 1
    assert {chunk.id for chunk in deltas} == {starts[0].id} == {ends[0].id}


async def test_segments_join_the_reply_as_blocks() -> None:
    chunks = await _chunks(
        [
            ResultSegmentEvent(segment=MarkdownSegment(data={"text": "first"})),
            ResultSegmentEvent(segment=MarkdownSegment(data={"text": "second"})),
        ]
    )

    deltas = [chunk.delta for chunk in chunks if isinstance(chunk, TextDeltaChunk)]
    assert deltas == ["first", "\n\nsecond"]


async def test_todo_events_surface_as_data_chunks() -> None:
    todo = Todo(conversation_id=uuid4(), ref="T1", content="Find the docs")

    chunks = await _chunks([TodoCreatedEvent(todo=todo)])

    data_chunks = [chunk for chunk in chunks if isinstance(chunk, DataChunk)]
    assert len(data_chunks) == 1
    assert data_chunks[0].type == "data-todo"
    assert data_chunks[0].transient
    assert data_chunks[0].data["event_kind"] == "todo_created"
    assert data_chunks[0].data["todo"]["ref"] == "T1"


async def test_action_batch_surfaces_as_data_chunk() -> None:
    question = DeferredQuestion(
        tool_name="ask_questions",
        tool_call_id="c1",
        args={"question": "Pick one?"},
    )

    chunks = await _chunks([ActionBatchEvent(batch_id="b1", questions=[question])])

    data_chunks = [chunk for chunk in chunks if isinstance(chunk, DataChunk)]
    assert len(data_chunks) == 1
    assert data_chunks[0].type == "data-action-batch"
    assert data_chunks[0].transient
    assert data_chunks[0].data["batch_id"] == "b1"
    assert data_chunks[0].data["questions"][0]["args"]["question"] == "Pick one?"


async def test_native_events_pass_through_to_stock_handlers() -> None:
    chunks = await _chunks(
        [PartStartEvent(index=0, part=ThinkingPart(content="hmm"))]
    )

    assert any(isinstance(chunk, ReasoningStartChunk) for chunk in chunks)
