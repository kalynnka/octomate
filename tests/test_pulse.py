"""Tests for PulseRunner — stepwise agent execution."""

import asyncio
import json

from pydantic_ai import Agent, ModelResponse, RunContext, ToolCallPart
from pydantic_ai.models.function import AgentInfo, FunctionModel
from pydantic_ai.tools import DeferredToolRequests
from pydantic_ai.toolsets import FunctionToolset

from octomate.agents.base import SessionContext
from octomate.agents.pulse import PulseRunner
from octomate.schemas.actions import AgentMessage
from octomate.schemas.session import SessionKey
from tests.helpers import (
    MockTentacle,
    make_octopus,
    make_private_event,
    rolling_loop,
    text_response_model,
)


def _session_key() -> SessionKey:
    return SessionKey(tentacle_id="t", user_id="u")


def _deps() -> SessionContext:
    return SessionContext(session_key=_session_key())


def _make_final_payload(text: str) -> str:
    return json.dumps(
        {"response": [{"segments": [{"type": "text", "data": {"text": text}}]}]}
    )


def multi_tool_model(tool_name: str, tool_arg: str, final_text: str) -> FunctionModel:
    """Model that calls a tool first, then produces a final text answer.

    On the first request (no tool return parts), it emits a tool call.
    On the second request (after the tool return), it emits the final output.
    """
    call_count = 0

    def fn(messages: list, info: AgentInfo) -> ModelResponse:
        nonlocal call_count
        call_count += 1

        if call_count == 1:
            return ModelResponse(
                parts=[
                    ToolCallPart(
                        tool_name=tool_name,
                        args=json.dumps({"text": tool_arg}),
                    )
                ]
            )

        output_tool = next(
            (t for t in info.output_tools if "deferred" not in t.name.lower()),
            info.output_tools[0] if info.output_tools else None,
        )
        payload = _make_final_payload(final_text)
        return ModelResponse(
            parts=[ToolCallPart(tool_name=output_tool.name, args=payload)]
        )

    return FunctionModel(fn)


def chained_tools_model(
    tool_calls: list[tuple[str, str]], final_text: str
) -> FunctionModel:
    """Model that chains N tool calls then produces a final answer.

    *tool_calls* is a list of (tool_name, tool_arg_json) pairs.
    """
    call_count = 0

    def fn(messages: list, info: AgentInfo) -> ModelResponse:
        nonlocal call_count
        call_count += 1

        if call_count <= len(tool_calls):
            name, arg = tool_calls[call_count - 1]
            return ModelResponse(
                parts=[ToolCallPart(tool_name=name, args=arg)]
            )

        output_tool = next(
            (t for t in info.output_tools if "deferred" not in t.name.lower()),
            info.output_tools[0] if info.output_tools else None,
        )
        payload = _make_final_payload(final_text)
        return ModelResponse(
            parts=[ToolCallPart(tool_name=output_tool.name, args=payload)]
        )

    return FunctionModel(fn)


def _echo_toolset() -> FunctionToolset[SessionContext]:
    toolset = FunctionToolset[SessionContext]()

    @toolset.tool
    async def echo(ctx: RunContext[SessionContext], text: str) -> str:
        """Echo back the text."""
        return f"echoed: {text}"

    return toolset


def _upper_toolset() -> FunctionToolset[SessionContext]:
    toolset = FunctionToolset[SessionContext]()

    @toolset.tool
    async def upper(ctx: RunContext[SessionContext], text: str) -> str:
        """Return the text in uppercase."""
        return text.upper()

    return toolset


# -------------------------------------------------------------------
# Unit tests for PulseRunner
# -------------------------------------------------------------------


async def test_pulse_single_step_response():
    """A simple text response completes in a single step."""
    agent = Agent(
        "test",
        deps_type=SessionContext,
        output_type=[list[AgentMessage], DeferredToolRequests],
    )
    runner = PulseRunner(agent)

    with agent.override(model=text_response_model("hello")):
        result = await runner.run("hi", deps=_deps())

    assert isinstance(result.output, list)
    assert len(result.output) == 1
    text = str(result.output[0])
    assert "hello" in text


async def test_pulse_two_step_tool_chain():
    """PulseRunner chains a tool call followed by a final answer."""
    toolset = _echo_toolset()
    agent = Agent(
        "test",
        deps_type=SessionContext,
        output_type=[list[AgentMessage], DeferredToolRequests],
        toolsets=[toolset],
    )
    runner = PulseRunner(agent)

    model = multi_tool_model("echo", "ping", "done")
    with agent.override(model=model):
        result = await runner.run("do echo", deps=_deps())

    assert isinstance(result.output, list)
    assert "done" in str(result.output[0])


async def test_pulse_three_step_tool_chain():
    """PulseRunner chains three tool calls then returns the final answer."""
    echo_ts = _echo_toolset()
    upper_ts = _upper_toolset()
    agent = Agent(
        "test",
        deps_type=SessionContext,
        output_type=[list[AgentMessage], DeferredToolRequests],
        toolsets=[echo_ts, upper_ts],
    )
    runner = PulseRunner(agent, max_steps=5)

    model = chained_tools_model(
        [
            ("echo", json.dumps({"text": "a"})),
            ("upper", json.dumps({"text": "b"})),
            ("echo", json.dumps({"text": "c"})),
        ],
        "all done",
    )
    with agent.override(model=model):
        result = await runner.run("chain please", deps=_deps())

    assert isinstance(result.output, list)
    assert "all done" in str(result.output[0])


async def test_pulse_max_steps_caps_execution():
    """When max_steps is reached, PulseRunner stops iterating and falls back to
    a direct run for a final answer, preventing runaway tool-call loops."""
    toolset = _echo_toolset()
    agent = Agent(
        "test",
        deps_type=SessionContext,
        output_type=[list[AgentMessage], DeferredToolRequests],
        toolsets=[toolset],
    )
    # Allow only 1 model-request step in the iter phase.  The model will call
    # echo once during iter, then the fallback run will produce the final answer.
    runner = PulseRunner(agent, max_steps=1)

    model = multi_tool_model("echo", "ping", "capped")
    with agent.override(model=model):
        result = await runner.run("lots of work", deps=_deps())

    assert isinstance(result.output, list)
    assert "capped" in str(result.output[0])


# -------------------------------------------------------------------
# Integration: PulseRunner through the full message loop
# -------------------------------------------------------------------


async def test_loop_with_pulse_tool_chain():
    """A multi-step tool chain through the full Octopus loop produces
    a single clean answer visible to the user."""
    octopus = make_octopus()
    tentacle = MockTentacle("test", octopus)
    octopus.connect(tentacle)

    echo_ts = _echo_toolset()
    model = multi_tool_model("echo", "ping", "result via pulse")

    with tentacle.flick.override(model=model):
        tentacle.pulse = PulseRunner(tentacle.flick)
        # attach the extra toolset so the model's tool call resolves
        original_toolsets = tentacle.toolsets
        tentacle.__dict__["toolsets"] = original_toolsets + [echo_ts]
        try:
            async with rolling_loop(octopus):
                tentacle.inject(make_private_event(text="do something"))
                await asyncio.sleep(0.1)
        finally:
            tentacle.__dict__.pop("toolsets", None)

    assert len(tentacle.sent) == 1
    _, segments = tentacle.sent[0]
    assert any(
        getattr(s, "data", {}).get("text") == "result via pulse" for s in segments
    )


async def test_loop_still_works_for_simple_response():
    """Sanity: PulseRunner doesn't break the simple text response path."""
    octopus = make_octopus()
    tentacle = MockTentacle("test", octopus)
    octopus.connect(tentacle)

    with tentacle.flick.override(model=text_response_model("simple")):
        async with rolling_loop(octopus):
            tentacle.inject(make_private_event(text="hello"))
            await asyncio.sleep(0.05)

    assert len(tentacle.sent) == 1
    _, segments = tentacle.sent[0]
    assert any(getattr(s, "data", {}).get("text") == "simple" for s in segments)
