"""Agent.stream_events: events + typed output in one stream."""

from __future__ import annotations

from pydantic import BaseModel
from pydantic_ai.messages import FunctionToolCallEvent
from pydantic_ai.models.test import TestModel
from pydantic_ai.result import FinalResult

from octomate.capabilities.agent import Agent
from octomate.capabilities.events import ResultSegmentEvent, ResultTextDeltaEvent
from octomate.schemas.segments import MessageSegment


class Row(BaseModel):
    name: str
    score: int


async def test_str_output_streams_text_deltas_then_final() -> None:
    agent = Agent(TestModel(custom_output_text="Hello there, world!"))

    events = [event async for event in agent.stream_events("hi")]

    deltas = [e.delta for e in events if isinstance(e, ResultTextDeltaEvent)]
    final = next(e for e in events if isinstance(e, FinalResult))
    assert deltas, "expected TextDeltaEvents (the typewriter)"
    assert "".join(deltas) == final.output == "Hello there, world!"
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


async def test_non_segment_structured_output_surfaces_only_at_final() -> None:
    agent = Agent(TestModel(), output_type=list[Row])

    events = [event async for event in agent.stream_events("go")]

    assert not any(isinstance(e, (ResultTextDeltaEvent, ResultSegmentEvent)) for e in events)
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
