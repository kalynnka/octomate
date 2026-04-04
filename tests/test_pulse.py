"""Tests for the Pulse graph-based planning and execution pipeline."""

from __future__ import annotations

from pydantic_ai import Agent, ModelResponse, TextPart
from pydantic_ai.models.function import AgentInfo, FunctionModel

from octomate.agents.pulse import (
    Classify,
    PulseDeps,
    PulseState,
    extract_text,
    parse_steps,
    pulse_graph,
    run_pulse,
)
from octomate.transmuters.interactions import Todo


# ---------------------------------------------------------------------------
# parse_steps unit tests
# ---------------------------------------------------------------------------


class TestParseSteps:
    def test_numbered_list(self):
        steps = parse_steps("1. Summarize X\n2. Compare Y\n3. Synthesize")
        assert len(steps) == 3
        assert steps[0] == "Summarize X"
        assert steps[2] == "Synthesize"

    def test_blank_lines_skipped(self):
        assert len(parse_steps("1. A\n\n2. B\n")) == 2

    def test_paren_numbering(self):
        assert parse_steps("1) Alpha\n2) Beta")[0] == "Alpha"

    def test_returns_empty_for_prose(self):
        assert parse_steps("I'm not sure what to do.") == []

    def test_returns_empty_for_empty(self):
        assert parse_steps("") == []


# ---------------------------------------------------------------------------
# extract_text
# ---------------------------------------------------------------------------


class TestExtractText:
    def test_string_passthrough(self):
        assert extract_text("hello") == "hello"

    def test_result_with_output_attr(self):
        class FakeResult:
            output = "value"

        assert extract_text(FakeResult()) == "value"


# ---------------------------------------------------------------------------
# FunctionModel helpers
# ---------------------------------------------------------------------------


def _text_model(text: str) -> FunctionModel:
    def fn(messages: list, info: AgentInfo) -> ModelResponse:
        return ModelResponse(parts=[TextPart(content=text)])

    return FunctionModel(fn)


# ---------------------------------------------------------------------------
# Classify: simple question → direct answer (no planning)
# ---------------------------------------------------------------------------


async def test_classify_direct_skips_planning():
    """Simple questions classified as 'direct' return an answer immediately."""
    call_count = 0

    def fn(messages: list, info: AgentInfo) -> ModelResponse:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return ModelResponse(parts=[TextPart(content="direct")])
        return ModelResponse(parts=[TextPart(content="42")])

    agent: Agent[None, str] = Agent("test", output_type=str)
    with agent.override(model=FunctionModel(fn)):
        result = await run_pulse(agent, deps=None, prompt="What is 6*7?")

    assert result == "42"
    assert call_count == 2  # classify + direct answer


# ---------------------------------------------------------------------------
# Classify → Decompose → ExecuteStep → Synthesize (full plan)
# ---------------------------------------------------------------------------


async def test_plan_path_runs_all_steps():
    """Complex tasks go through classify → decompose → execute → synthesize."""
    call_count = 0

    def fn(messages: list, info: AgentInfo) -> ModelResponse:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return ModelResponse(parts=[TextPart(content="plan")])
        if call_count == 2:
            return ModelResponse(
                parts=[TextPart(content="1. Step A\n2. Step B")]
            )
        return ModelResponse(parts=[TextPart(content=f"result-{call_count}")])

    agent: Agent[None, str] = Agent("test", output_type=str)
    with agent.override(model=FunctionModel(fn)):
        result = await run_pulse(agent, deps=None, prompt="Summarize then compare")

    # 1 classify + 1 decompose + 2 steps + 1 synthesize = 5
    assert call_count == 5
    assert isinstance(result, str)
    assert len(result) > 0


# ---------------------------------------------------------------------------
# Steps are tracked as Todo objects
# ---------------------------------------------------------------------------


async def test_steps_tracked_as_todos():
    """Plan steps in the graph state are Todo objects from the existing transmuter."""
    call_count = 0

    def fn(messages: list, info: AgentInfo) -> ModelResponse:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return ModelResponse(parts=[TextPart(content="plan")])
        if call_count == 2:
            return ModelResponse(
                parts=[TextPart(content="1. Summarize\n2. Compare")]
            )
        return ModelResponse(parts=[TextPart(content="done")])

    agent: Agent[None, str] = Agent("test", output_type=str)
    state = PulseState(goal="test task")
    pulse_deps = PulseDeps(agent=agent, agent_deps=None)

    with agent.override(model=FunctionModel(fn)):
        await pulse_graph.run(Classify(), state=state, deps=pulse_deps)

    assert len(state.todos) == 2
    assert all(isinstance(t, Todo) for t in state.todos)
    assert all(t.status == "done" for t in state.todos)
    assert state.todos[0].title == "Summarize"
    assert state.todos[1].title == "Compare"


# ---------------------------------------------------------------------------
# Decompose fallback on unparseable response
# ---------------------------------------------------------------------------


async def test_decompose_fallback_on_unparseable():
    """When decomposition returns non-numbered text, falls back to direct answer."""
    call_count = 0

    def fn(messages: list, info: AgentInfo) -> ModelResponse:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return ModelResponse(parts=[TextPart(content="plan")])
        if call_count == 2:
            return ModelResponse(
                parts=[TextPart(content="I can just answer this directly.")]
            )
        return ModelResponse(parts=[TextPart(content="direct answer")])

    agent: Agent[None, str] = Agent("test", output_type=str)
    with agent.override(model=FunctionModel(fn)):
        result = await run_pulse(agent, deps=None, prompt="simple question")

    # 1 classify + 1 decompose (unparseable) + 1 direct answer = 3
    assert call_count == 3
    assert result == "direct answer"


# ---------------------------------------------------------------------------
# Full end-to-end pipeline: no internal leakage
# ---------------------------------------------------------------------------


async def test_full_pipeline_no_internal_leakage():
    """Internal plan details must not leak into the final answer."""
    call_count = 0

    def fn(messages: list, info: AgentInfo) -> ModelResponse:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return ModelResponse(parts=[TextPart(content="plan")])
        if call_count == 2:
            return ModelResponse(
                parts=[
                    TextPart(content="1. Summarize\n2. Compare\n3. Synthesize")
                ]
            )
        if call_count <= 5:
            return ModelResponse(
                parts=[TextPart(content=f"Step output {call_count}")]
            )
        return ModelResponse(parts=[TextPart(content="Final clean answer")])

    agent: Agent[None, str] = Agent("test", output_type=str)
    with agent.override(model=FunctionModel(fn)):
        answer = await run_pulse(agent, deps=None, prompt="Analyze the report")

    assert answer == "Final clean answer"
    # 1 classify + 1 decompose + 3 steps + 1 synthesize = 6
    assert call_count == 6


async def test_run_pulse_with_list_prompt():
    """run_pulse accepts a list prompt and flattens it."""
    agent: Agent[None, str] = Agent("test", output_type=str)
    with agent.override(model=_text_model("direct")):
        # Classify returns "direct", then we need a second call for the answer
        call_count = 0

        def fn(messages: list, info: AgentInfo) -> ModelResponse:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return ModelResponse(parts=[TextPart(content="direct")])
            return ModelResponse(parts=[TextPart(content="ok")])

        with agent.override(model=FunctionModel(fn)):
            result = await run_pulse(
                agent, deps=None, prompt=["hello", "world"]
            )
            assert result == "ok"
