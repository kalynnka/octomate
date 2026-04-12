"""Tests for the Pulse graph-based planning and execution pipeline."""

from __future__ import annotations

from pydantic_ai import Agent, ModelResponse, TextPart
from pydantic_ai.models.function import AgentInfo, FunctionModel

from octomate.agents.pulse import (
    PulseDeps,
    PulseState,
    Triage,
    parse_steps,
    pulse_graph,
    run_pulse,
)
from octomate.transmuters.interactions import Todo


class TestParseSteps:
    def test_numbered_list(self):
        steps = parse_steps("1. Summarize X\n2. Compare Y\n3. Synthesize")
        assert len(steps) == 3
        assert steps[0] == "Summarize X"
        assert steps[2] == "Synthesize"

    def test_blank_lines_skipped(self):
        steps = parse_steps("1. A\n\n2. B\n")
        assert len(steps) == 2
        assert steps[0] == "A"
        assert steps[1] == "B"

    def test_paren_numbering(self):
        assert parse_steps("1) Alpha\n2) Beta")[0] == "Alpha"

    def test_returns_empty_for_prose(self):
        assert parse_steps("I'm not sure what to do.") == []

    def test_returns_empty_for_empty(self):
        assert parse_steps("") == []


def _text_model(text: str) -> FunctionModel:
    def fn(messages: list, info: AgentInfo) -> ModelResponse:
        return ModelResponse(parts=[TextPart(content=text)])

    return FunctionModel(fn)


async def test_direct_answer_single_call():
    """Simple questions answered directly require only 1 LLM call."""
    call_count = 0

    def fn(messages: list, info: AgentInfo) -> ModelResponse:
        nonlocal call_count
        call_count += 1
        return ModelResponse(parts=[TextPart(content="42")])

    agent: Agent[None, str] = Agent("test", output_type=str)
    with agent.override(model=FunctionModel(fn)):
        result = await run_pulse(agent, deps=None, prompt="What is 6*7?")

    assert result == "42"
    assert call_count == 1


async def test_plan_path_runs_all_steps():
    """Complex tasks go through triage → execute steps → synthesize."""
    call_count = 0

    def fn(messages: list, info: AgentInfo) -> ModelResponse:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return ModelResponse(
                parts=[TextPart(content="1. Step A\n2. Step B")]
            )
        return ModelResponse(parts=[TextPart(content=f"result-{call_count}")])

    agent: Agent[None, str] = Agent("test", output_type=str)
    with agent.override(model=FunctionModel(fn)):
        result = await run_pulse(agent, deps=None, prompt="Summarize then compare")

    # 1 triage + 2 steps + 1 synthesize = 4
    assert call_count == 4
    assert isinstance(result, str)
    assert len(result) > 0


async def test_steps_tracked_as_todos():
    """Plan steps in the graph state are Todo objects from the existing transmuter."""
    call_count = 0

    def fn(messages: list, info: AgentInfo) -> ModelResponse:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return ModelResponse(
                parts=[TextPart(content="1. Summarize\n2. Compare")]
            )
        return ModelResponse(parts=[TextPart(content="done")])

    agent: Agent[None, str] = Agent("test", output_type=str)
    state = PulseState(goal="test task")
    pulse_deps = PulseDeps(agent=agent, agent_deps=None)

    with agent.override(model=FunctionModel(fn)):
        await pulse_graph.run(Triage(), state=state, deps=pulse_deps)

    assert len(state.todos) == 2
    assert all(isinstance(t, Todo) for t in state.todos)
    assert all(t.status == "done" for t in state.todos)
    assert state.todos[0].title == "Summarize"
    assert state.todos[1].title == "Compare"


async def test_triage_fallback_to_direct():
    """When triage returns non-numbered text, it becomes the direct answer."""
    agent: Agent[None, str] = Agent("test", output_type=str)
    with agent.override(model=_text_model("Just a simple answer.")):
        result = await run_pulse(agent, deps=None, prompt="simple question")

    assert result == "Just a simple answer."


async def test_full_pipeline_no_internal_leakage():
    """Internal plan details must not leak into the final answer."""
    call_count = 0

    def fn(messages: list, info: AgentInfo) -> ModelResponse:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return ModelResponse(
                parts=[
                    TextPart(content="1. Summarize\n2. Compare\n3. Synthesize")
                ]
            )
        if call_count <= 4:
            return ModelResponse(
                parts=[TextPart(content=f"Step output {call_count}")]
            )
        return ModelResponse(parts=[TextPart(content="Final clean answer")])

    agent: Agent[None, str] = Agent("test", output_type=str)
    with agent.override(model=FunctionModel(fn)):
        answer = await run_pulse(agent, deps=None, prompt="Analyze the report")

    assert answer == "Final clean answer"
    # 1 triage + 3 steps + 1 synthesize = 5
    assert call_count == 5


async def test_run_pulse_with_list_prompt():
    """run_pulse accepts a list prompt and flattens it."""
    agent: Agent[None, str] = Agent("test", output_type=str)
    with agent.override(model=_text_model("ok")):
        result = await run_pulse(agent, deps=None, prompt=["hello", "world"])
        assert result == "ok"
