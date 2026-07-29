"""A failing tool is reported to the model, not allowed to end the turn."""

from __future__ import annotations

from collections.abc import AsyncIterator

from pydantic_ai import AgentRunResultEvent
from pydantic_ai.messages import ModelMessage, ToolCallPart, ToolReturnPart
from pydantic_ai.models.function import (
    AgentInfo,
    DeltaToolCall,
    DeltaToolCalls,
    FunctionModel,
)
from pydantic_ai.toolsets import FunctionToolset

from octomate.capabilities.harness.agent import Agent
from octomate.capabilities.tools import ToolFailureCapability

TOOL = "fetch_the_thing"


def calls_of(messages: list[ModelMessage]) -> int:
    return sum(
        1
        for message in messages
        for part in message.parts
        if isinstance(part, ToolCallPart) and part.tool_name == TOOL
    )


def broken_agent(*, give_up_after: int) -> Agent[None, str]:
    """An agent whose one tool always fails, and a model that tries it
    `give_up_after` times before answering anyway."""
    toolset: FunctionToolset[None] = FunctionToolset()

    @toolset.tool_plain
    def fetch_the_thing() -> str:
        """Fetch it."""
        raise RuntimeError("upstream exploded")

    async def scripted(
        messages: list[ModelMessage], info: AgentInfo
    ) -> AsyncIterator[str | DeltaToolCalls]:
        if calls_of(messages) < give_up_after:
            yield {0: DeltaToolCall(name=TOOL, json_args="{}")}
        else:
            yield "I could not fetch it: upstream exploded."

    return Agent(
        FunctionModel(stream_function=scripted, model_name="scripted"),
        deps_type=type(None),
        toolsets=[toolset],
        capabilities=[ToolFailureCapability()],
    )


async def test_a_failing_tool_is_reported_and_the_run_still_answers() -> None:
    agent = broken_agent(give_up_after=1)

    events = [event async for event in agent.stream_events("fetch it")]

    result = next(e for e in events if isinstance(e, AgentRunResultEvent)).result
    # The turn reached the person instead of dying on the failed call — and the
    # answer is the model's own account of what went wrong.
    assert result.output == "I could not fetch it: upstream exploded."
    failures = [
        part
        for message in result.all_messages()
        for part in message.parts
        if isinstance(part, ToolReturnPart) and part.outcome == "failed"
    ]
    assert len(failures) == 1
    assert "upstream exploded" in str(failures[0].content)


async def test_one_tool_failing_twice_does_not_end_the_run() -> None:
    # The shape that used to lose a turn: a tool fails, the model tries again, and
    # the second failure spends the retry budget (1 by default) — which ended the
    # run. A reported failure carries no such budget, so the model still answers.
    agent = broken_agent(give_up_after=2)

    events = [event async for event in agent.stream_events("fetch it")]

    result = next(e for e in events if isinstance(e, AgentRunResultEvent)).result
    assert result.output == "I could not fetch it: upstream exploded."
    assert calls_of(list(result.all_messages())) == 2
