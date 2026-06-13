"""Agent.stream_events: events + typed output in one stream."""

from __future__ import annotations

from collections.abc import AsyncIterable, AsyncIterator
from dataclasses import dataclass
from typing import cast

from pydantic import BaseModel
from pydantic_ai import AgentRunResultEvent, AgentStreamEvent, RunContext
from pydantic_ai.capabilities import AbstractCapability
from pydantic_ai.messages import (
    FunctionToolCallEvent,
    ModelMessage,
    PartDeltaEvent,
    PartStartEvent,
    ThinkingPart,
    TextPart,
    TextPartDelta,
)
from pydantic_ai.models.function import (
    AgentInfo,
    DeltaThinkingCalls,
    DeltaThinkingPart,
    DeltaToolCall,
    DeltaToolCalls,
    FunctionModel,
)
from pydantic_ai.models.test import TestModel
from pydantic_ai.result import FinalResult
from pydantic_ai.toolsets import FunctionToolset
from uuid_utils.compat import uuid7

from octomate.capabilities.agent import Agent
from octomate.capabilities.events import (
    ResultSegmentEvent,
    TodoCreatedEvent,
)
from octomate.schemas.segments import MessageSegment
from octomate.schemas.todos import Todo


class Row(BaseModel):
    name: str
    score: int


async def test_str_output_streams_native_text_events_then_final() -> None:
    async def stream_text(
        messages: list[ModelMessage], info: AgentInfo
    ) -> AsyncIterator[str]:
        yield "Hello there, "
        yield "world!"

    agent = Agent(FunctionModel(stream_function=stream_text, model_name="scripted"))

    events = [event async for event in agent.stream_events("hi")]

    deltas: list[str] = []
    for event in events:
        match event:
            case PartStartEvent(part=TextPart(content=content)):
                deltas.append(content)
            case PartDeltaEvent(delta=TextPartDelta(content_delta=delta)):
                deltas.append(delta)
    final = next(e for e in events if isinstance(e, FinalResult))
    assert deltas, "expected native text events"
    assert final.output == "Hello there, world!"
    # Every text delta must stream, not just the first one before FinalResultEvent.
    assert "".join(deltas) == final.output
    assert any(type(e).__name__ == "AgentRunResultEvent" for e in events)


async def test_segment_output_streams_one_event_per_segment() -> None:
    reply = [
        {"type": "text", "data": {"text": "hello"}},
        {"type": "markdown", "data": {"text": "**hi**"}},
        {"type": "at", "data": {"user_id": "u1"}},
    ]
    agent = Agent(
        TestModel(custom_output_args=reply), output_type=list[MessageSegment]
    )

    events = [event async for event in agent.stream_events("go")]

    streamed = [e.segment for e in events if isinstance(e, ResultSegmentEvent)]
    final = next(e for e in events if isinstance(e, FinalResult))
    assert [segment.type for segment in streamed] == ["text", "markdown", "at"]
    assert streamed == final.output


async def test_segment_output_streams_each_segment_exactly_once() -> None:
    """Multi-round partial validation: a segment streams only once a later one has
    sealed it; the trailing segment arrives from the final validated output —
    never truncated mid-growth, never duplicated."""

    fragments = [
        '{"response": [{"type": "text", "data": {"text": "one"}},',
        ' {"type": "markdown", "data": {"text": "tw',
        'o"}}]}',
    ]

    async def stream_args(
        messages: list[ModelMessage], info: AgentInfo
    ) -> AsyncIterator[DeltaToolCalls]:
        yield {0: DeltaToolCall(name="final_result", json_args=fragments[0])}
        for fragment in fragments[1:]:
            yield {0: DeltaToolCall(json_args=fragment)}

    agent = Agent(
        FunctionModel(stream_function=stream_args, model_name="scripted"),
        output_type=list[MessageSegment],
    )

    events = [event async for event in agent.stream_events("go")]

    streamed = [e.segment for e in events if isinstance(e, ResultSegmentEvent)]
    final = next(e for e in events if isinstance(e, FinalResult))
    assert [str(segment) for segment in streamed] == ["one", "two"]
    assert streamed == final.output


async def test_non_segment_structured_output_surfaces_only_at_final() -> None:
    agent = Agent(TestModel(), output_type=list[Row])

    events = [event async for event in agent.stream_events("go")]

    assert not any(isinstance(e, ResultSegmentEvent) for e in events)
    final = next(e for e in events if isinstance(e, FinalResult))
    assert isinstance(final.output, list)
    assert all(isinstance(row, Row) for row in final.output)


async def test_tool_call_events_pass_through() -> None:
    agent = Agent(TestModel())

    @agent.tool_plain
    def ping() -> str:
        return "pong"

    events = [event async for event in agent.stream_events("use ping")]

    assert any(isinstance(e, FunctionToolCallEvent) for e in events)
    assert any(isinstance(e, FinalResult) for e in events)


async def test_call_time_run_params_are_forwarded() -> None:
    """`stream_events` forwards the per-run kwargs to `iter()` — a call-time
    toolset must reach the model (TestModel calls every tool it can see)."""

    agent = Agent(TestModel())

    def ping() -> str:
        return "pong"

    events = [
        event
        async for event in agent.stream_events(
            "use ping", toolsets=[FunctionToolset(tools=[ping])]
        )
    ]

    calls = [e for e in events if isinstance(e, FunctionToolCallEvent)]
    assert any(e.part.tool_name == "ping" for e in calls)


async def test_thinking_events_pass_through_before_final() -> None:
    async def stream_thinking_then_text(
        messages: list[ModelMessage], info: AgentInfo
    ) -> AsyncIterator[str | DeltaThinkingCalls]:
        yield {0: DeltaThinkingPart(content="pondering the request")}
        yield "Hello there"

    agent = Agent(
        FunctionModel(stream_function=stream_thinking_then_text, model_name="thinker")
    )

    events = [event async for event in agent.stream_events("hi")]

    thinking_indices = [
        index
        for index, event in enumerate(events)
        if isinstance(event, PartStartEvent) and isinstance(event.part, ThinkingPart)
    ]
    final_index = next(
        index for index, event in enumerate(events) if isinstance(event, FinalResult)
    )
    assert thinking_indices, "expected thinking Part events on the stream"
    assert all(index < final_index for index in thinking_indices)


@dataclass
class InjectingCapability(AbstractCapability[None]):
    """Minimal capability that injects one extra event onto the run stream,
    mirroring how TodoCapability forwards stashed todo events."""

    event: TodoCreatedEvent

    async def wrap_run_event_stream(
        self,
        ctx: RunContext[None],
        *,
        stream: AsyncIterable[AgentStreamEvent],
    ) -> AsyncIterable[AgentStreamEvent]:
        # The injected display event is not an AgentStreamEvent; one
        # dynamic-boundary cast, exactly as TodoCapability does.
        yield cast(AgentStreamEvent, self.event)
        async for event in stream:
            yield event


async def test_capability_injected_events_reach_consumer() -> None:
    injected = TodoCreatedEvent(
        todo=Todo(conversation_id=uuid7(), ref="r1", content="ship it")
    )
    agent = Agent(
        TestModel(custom_output_text="ok"),
        capabilities=[InjectingCapability(event=injected)],
    )

    events = [event async for event in agent.stream_events("go")]

    assert any(event is injected for event in events)


async def test_terminal_agent_run_result_event_closes_stream() -> None:
    agent = Agent(TestModel(custom_output_text="done"))

    events = [event async for event in agent.stream_events("hi")]

    # The async-for completed, so the stream closed — and its last event is the
    # terminal AgentRunResultEvent, exactly once.
    assert isinstance(events[-1], AgentRunResultEvent)
    assert sum(isinstance(event, AgentRunResultEvent) for event in events) == 1
