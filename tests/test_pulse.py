"""Tests for the Pulse graph-based planning and execution pipeline."""

from __future__ import annotations

from typing import Any

from pydantic_ai import Agent, ModelResponse, TextPart, ToolCallPart
from pydantic_ai.models.function import AgentInfo, FunctionModel

from octomate.agents.pulse import (
    PulseDeps,
    PulseState,
    Triage,
    _build_triage_output_type,
    _is_todo_plan,
    pulse_graph,
    run_pulse,
)
from octomate.transmuters.interactions import Todo


def _text_model(text: str) -> FunctionModel:
    def fn(messages: list, info: AgentInfo) -> ModelResponse:
        return ModelResponse(parts=[TextPart(content=text)])

    return FunctionModel(fn)


def _todo_tool_call(todos: list[dict[str, str]]) -> ModelResponse:
    """Build a ModelResponse that invokes the list[Todo] structured output tool."""
    return ModelResponse(
        parts=[
            ToolCallPart(
                tool_name="final_result",
                args={"response": todos},
                tool_call_id="plan-call",
            )
        ]
    )


class TestBuildTriageOutputType:
    def test_single_output_type(self):
        agent: Agent[None, str] = Agent("test", output_type=str)
        types = _build_triage_output_type(agent)
        assert str in types
        assert list[Todo] in types

    def test_list_output_type(self):
        agent: Agent[None, str] = Agent("test", output_type=[str, int])
        types = _build_triage_output_type(agent)
        assert str in types
        assert int in types
        assert list[Todo] in types


class TestIsTodoPlan:
    def test_empty_list(self):
        assert not _is_todo_plan([])

    def test_non_list(self):
        assert not _is_todo_plan("hello")

    def test_list_of_non_todo(self):
        assert not _is_todo_plan(["step1", "step2"])

    def test_list_of_todos(self):
        todos = [Todo(todo_id="t1", title="Step")]
        assert _is_todo_plan(todos)


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
    todos = [
        {"todo_id": "pulse-0", "title": "Step A", "description": "Do step A in detail"},
        {"todo_id": "pulse-1", "title": "Step B", "description": "Do step B in detail"},
    ]

    def fn(messages: list, info: AgentInfo) -> ModelResponse:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return _todo_tool_call(todos)
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
    todos = [
        {"todo_id": "pulse-0", "title": "Summarize", "description": "Summarize the document"},
        {"todo_id": "pulse-1", "title": "Compare", "description": "Compare against last quarter"},
    ]

    call_count = 0

    def fn(messages: list, info: AgentInfo) -> ModelResponse:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return _todo_tool_call(todos)
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
    todos = [
        {"todo_id": "pulse-0", "title": "Summarize", "description": "Summarize key findings"},
        {"todo_id": "pulse-1", "title": "Compare", "description": "Compare with baseline"},
        {"todo_id": "pulse-2", "title": "Recommend", "description": "Produce final recommendation"},
    ]

    def fn(messages: list, info: AgentInfo) -> ModelResponse:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return _todo_tool_call(todos)
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


async def test_synthesize_uses_native_output_type():
    """Synthesize returns in the agent's configured output type, not str."""
    call_count = 0
    todos = [
        {"todo_id": "pulse-0", "title": "Research", "description": "Research the topic"},
        {"todo_id": "pulse-1", "title": "Write", "description": "Write the summary"},
    ]

    def fn(messages: list, info: AgentInfo) -> ModelResponse:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return _todo_tool_call(todos)
        return ModelResponse(parts=[TextPart(content="native output")])

    agent: Agent[None, str] = Agent("test", output_type=str)
    with agent.override(model=FunctionModel(fn)):
        result = await run_pulse(agent, deps=None, prompt="Research and summarize")

    assert isinstance(result, str)
    # 1 triage + 2 steps + 1 synthesize = 4
    assert call_count == 4
