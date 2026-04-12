"""Tests for the Pulse graph-based planning and execution pipeline."""

from __future__ import annotations

import json

from pydantic_ai import Agent, ModelResponse, TextPart, ToolCallPart, ToolReturnPart
from pydantic_ai.models.function import AgentInfo, FunctionModel

from octomate.agents.pulse import (
    PulseDeps,
    PulsePlan,
    PulseState,
    PulseStep,
    Triage,
    pulse_graph,
    run_pulse,
)
from octomate.transmuters.interactions import Todo


def _text_model(text: str) -> FunctionModel:
    def fn(messages: list, info: AgentInfo) -> ModelResponse:
        return ModelResponse(parts=[TextPart(content=text)])

    return FunctionModel(fn)


def _plan_tool_call(steps: list[dict]) -> ModelResponse:
    """Build a ModelResponse that invokes the PulsePlan structured output tool."""
    plan = PulsePlan(steps=[PulseStep(**s) for s in steps])
    return ModelResponse(
        parts=[
            ToolCallPart(
                tool_name="final_result",
                args=plan.model_dump(),
                tool_call_id="plan-call",
            )
        ]
    )


def _plan_then_text_model(steps: list[dict]) -> FunctionModel:
    """First call returns a PulsePlan tool call; subsequent calls return text."""
    call_count = 0

    def fn(messages: list, info: AgentInfo) -> ModelResponse:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return _plan_tool_call(steps)
        return ModelResponse(parts=[TextPart(content=f"result-{call_count}")])

    return FunctionModel(fn)


class TestPulsePlan:
    def test_plan_roundtrip(self):
        plan = PulsePlan(
            steps=[
                PulseStep(title="Summarize", description="Summarize the key points"),
                PulseStep(title="Compare", description="Compare against benchmarks"),
            ]
        )
        assert len(plan.steps) == 2
        assert plan.steps[0].title == "Summarize"
        assert plan.steps[1].description == "Compare against benchmarks"

    def test_plan_json_schema(self):
        schema = PulsePlan.model_json_schema()
        assert "steps" in schema["properties"]


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
    steps = [
        {"title": "Step A", "description": "Do step A in detail"},
        {"title": "Step B", "description": "Do step B in detail"},
    ]

    def fn(messages: list, info: AgentInfo) -> ModelResponse:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return _plan_tool_call(steps)
        return ModelResponse(parts=[TextPart(content=f"result-{call_count}")])

    agent: Agent[None, str] = Agent("test", output_type=str)
    with agent.override(model=FunctionModel(fn)):
        result = await run_pulse(agent, deps=None, prompt="Summarize then compare")

    # 1 triage + 2 steps + 1 synthesize = 4
    assert call_count == 4
    assert isinstance(result, str)
    assert len(result) > 0


async def test_steps_tracked_as_todos():
    """Plan steps in the graph state are Todo objects with title and description."""
    steps = [
        {"title": "Summarize", "description": "Summarize the document"},
        {"title": "Compare", "description": "Compare against last quarter"},
    ]

    call_count = 0

    def fn(messages: list, info: AgentInfo) -> ModelResponse:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return _plan_tool_call(steps)
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
    assert state.todos[0].description == "Summarize the document"
    assert state.todos[1].title == "Compare"
    assert state.todos[1].description == "Compare against last quarter"


async def test_triage_fallback_to_direct():
    """When triage returns plain text, it becomes the direct answer."""
    agent: Agent[None, str] = Agent("test", output_type=str)
    with agent.override(model=_text_model("Just a simple answer.")):
        result = await run_pulse(agent, deps=None, prompt="simple question")

    assert result == "Just a simple answer."


async def test_full_pipeline_no_internal_leakage():
    """Internal plan details must not leak into the final answer."""
    call_count = 0
    steps = [
        {"title": "Summarize", "description": "Summarize key findings"},
        {"title": "Compare", "description": "Compare with baseline"},
        {"title": "Synthesize", "description": "Produce final recommendation"},
    ]

    def fn(messages: list, info: AgentInfo) -> ModelResponse:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return _plan_tool_call(steps)
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


async def test_todo_description_propagated_to_step():
    """The description from the plan is stored in Todo and used by ExecuteStep."""
    steps = [
        {"title": "Research", "description": "Research the topic in depth using all sources"},
        {"title": "Summarize", "description": "Write a brief summary"},
    ]

    call_count = 0

    def fn(messages: list, info: AgentInfo) -> ModelResponse:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return _plan_tool_call(steps)
        return ModelResponse(parts=[TextPart(content="done")])

    agent: Agent[None, str] = Agent("test", output_type=str)
    state = PulseState(goal="Do research")
    pulse_deps = PulseDeps(agent=agent, agent_deps=None)

    with agent.override(model=FunctionModel(fn)):
        await pulse_graph.run(Triage(), state=state, deps=pulse_deps)

    assert state.todos[0].description == "Research the topic in depth using all sources"
    assert state.todos[1].description == "Write a brief summary"
