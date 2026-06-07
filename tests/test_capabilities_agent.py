"""Agent.stream_events: events + typed output in one stream."""

from __future__ import annotations

from pydantic import BaseModel
from pydantic_ai.messages import FunctionToolCallEvent
from pydantic_ai.models.test import TestModel
from pydantic_ai.result import FinalResult

from octomate.capabilities.agent import Agent
from octomate.capabilities.events import OutputDeltaEvent


class Row(BaseModel):
    name: str
    score: int


async def test_str_output_streams_output_events_then_final() -> None:
    agent = Agent(TestModel())

    events = [event async for event in agent.stream_events("hi")]

    kinds = [type(e).__name__ for e in events]
    assert "OutputDeltaEvent" in kinds
    assert "FinalResult" in kinds
    assert "AgentRunResultEvent" in kinds
    final = next(e for e in events if isinstance(e, FinalResult))
    assert isinstance(final.output, str)


async def test_structured_output_streams_validated_rows() -> None:
    agent = Agent(TestModel(), output_type=list[Row])

    events = [event async for event in agent.stream_events("go")]

    deltas = [e for e in events if isinstance(e, OutputDeltaEvent)]
    final = next(e for e in events if isinstance(e, FinalResult))
    assert deltas, "expected at least one OutputDeltaEvent (partial rows)"
    assert isinstance(deltas[-1].output, list)
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
