"""Tests for the Pulse graph-based planning pipeline."""

from __future__ import annotations

from pydantic_ai import Agent, ModelResponse, TextPart, ToolCallPart
from pydantic_ai.models.function import AgentInfo, FunctionModel

from octomate.agents.pulse import PulseAgents
from octomate.agents.pulse import (
    PulseDeps,
    PulseState,
    Triage,
    pulse_graph,
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


def _make_agents() -> PulseAgents:
    """Create test agents with simple output types."""
    triage: Agent[None, str] = Agent("test", output_type=[str, list[Todo]])
    step: Agent[None, str] = Agent("test", output_type=str)
    synth: Agent[None, str] = Agent("test", output_type=str)
    return PulseAgents(triage=triage, step=step, synthesize=synth)


def _make_deps(agents: PulseAgents) -> PulseDeps:
    return PulseDeps(
        triage=agents.triage,
        step=agents.step,
        synthesize=agents.synthesize,
        agent_deps=None,  # type: ignore[arg-type]  # test agents don't use deps
        tentacle=None,  # type: ignore[arg-type]  # test agents don't use deferred tools
    )


async def _run(agents: PulseAgents, prompt: str | list):
    state = PulseState(prompt=prompt)
    result = await pulse_graph.run(Triage(), state=state, deps=_make_deps(agents))
    return result.output


async def test_direct_answer_single_call():
    """Simple questions answered directly require only 1 LLM call."""
    call_count = 0

    def fn(messages: list, info: AgentInfo) -> ModelResponse:
        nonlocal call_count
        call_count += 1
        return ModelResponse(parts=[TextPart(content="42")])

    agents = _make_agents()
    with agents.triage.override(model=FunctionModel(fn)):
        result = await _run(agents, "What is 6*7?")

    assert result == "42"
    assert call_count == 1


async def test_plan_path_runs_all_steps():
    """Complex tasks go through triage → execute steps → synthesize."""
    call_count = 0
    todos = [
        {"todo_id": "pulse-0", "title": "Step A", "description": "Do step A in detail"},
        {"todo_id": "pulse-1", "title": "Step B", "description": "Do step B in detail"},
    ]

    def triage_fn(messages: list, info: AgentInfo) -> ModelResponse:
        nonlocal call_count
        call_count += 1
        return _todo_tool_call(todos)

    def work_fn(messages: list, info: AgentInfo) -> ModelResponse:
        nonlocal call_count
        call_count += 1
        return ModelResponse(parts=[TextPart(content=f"result-{call_count}")])

    agents = _make_agents()
    with (
        agents.triage.override(model=FunctionModel(triage_fn)),
        agents.step.override(model=FunctionModel(work_fn)),
        agents.synthesize.override(model=FunctionModel(work_fn)),
    ):
        result = await _run(agents, "Summarize then compare")

    # 1 triage + 2 steps + 1 synthesize = 4
    assert call_count == 4
    assert isinstance(result, str)
    assert len(result) > 0


async def test_steps_tracked_as_todos():
    """Plan steps in the graph state are Todo objects marked done after execution."""
    todos = [
        {"todo_id": "pulse-0", "title": "Summarize", "description": "Summarize the document"},
        {"todo_id": "pulse-1", "title": "Compare", "description": "Compare against last quarter"},
    ]

    call_count = 0

    def triage_fn(messages: list, info: AgentInfo) -> ModelResponse:
        nonlocal call_count
        call_count += 1
        return _todo_tool_call(todos)

    agents = _make_agents()
    state = PulseState(prompt="test task")

    with (
        agents.triage.override(model=FunctionModel(triage_fn)),
        agents.step.override(model=_text_model("done")),
        agents.synthesize.override(model=_text_model("done")),
    ):
        await pulse_graph.run(Triage(), state=state, deps=_make_deps(agents))

    assert len(state.todos) == 2
    assert all(isinstance(t, Todo) for t in state.todos)
    assert all(t.status == "done" for t in state.todos)
    assert state.todos[0].title == "Summarize"
    assert state.todos[0].description == "Summarize the document"
    assert state.todos[1].title == "Compare"
    assert state.todos[1].description == "Compare against last quarter"


async def test_triage_fallback_to_direct():
    """When triage returns plain text, it becomes the direct answer."""
    agents = _make_agents()
    with agents.triage.override(model=_text_model("Just a simple answer.")):
        result = await _run(agents, "simple question")

    assert result == "Just a simple answer."


async def test_full_pipeline_no_internal_leakage():
    """Internal plan details must not leak into the final answer."""
    call_count = 0
    todos = [
        {"todo_id": "pulse-0", "title": "Summarize", "description": "Summarize key findings"},
        {"todo_id": "pulse-1", "title": "Compare", "description": "Compare with baseline"},
        {"todo_id": "pulse-2", "title": "Recommend", "description": "Produce final recommendation"},
    ]

    def triage_fn(messages: list, info: AgentInfo) -> ModelResponse:
        nonlocal call_count
        call_count += 1
        return _todo_tool_call(todos)

    def work_fn(messages: list, info: AgentInfo) -> ModelResponse:
        nonlocal call_count
        call_count += 1
        if call_count <= 4:
            return ModelResponse(parts=[TextPart(content=f"Step output {call_count}")])
        return ModelResponse(parts=[TextPart(content="Final clean answer")])

    agents = _make_agents()
    with (
        agents.triage.override(model=FunctionModel(triage_fn)),
        agents.step.override(model=FunctionModel(work_fn)),
        agents.synthesize.override(model=FunctionModel(work_fn)),
    ):
        answer = await _run(agents, "Analyze the report")

    assert answer == "Final clean answer"
    # 1 triage + 3 steps + 1 synthesize = 5
    assert call_count == 5


async def test_list_prompt():
    """pulse_graph accepts a list prompt and flattens it for steps."""
    agents = _make_agents()
    with agents.triage.override(model=_text_model("ok")):
        result = await _run(agents, ["hello", "world"])
        assert result == "ok"


async def test_synthesize_uses_native_output_type():
    """Synthesize returns in the synth agent's configured output type."""
    call_count = 0
    todos = [
        {"todo_id": "pulse-0", "title": "Research", "description": "Research the topic"},
        {"todo_id": "pulse-1", "title": "Write", "description": "Write the summary"},
    ]

    def triage_fn(messages: list, info: AgentInfo) -> ModelResponse:
        nonlocal call_count
        call_count += 1
        return _todo_tool_call(todos)

    def work_fn(messages: list, info: AgentInfo) -> ModelResponse:
        nonlocal call_count
        call_count += 1
        return ModelResponse(parts=[TextPart(content="native output")])

    agents = _make_agents()
    with (
        agents.triage.override(model=FunctionModel(triage_fn)),
        agents.step.override(model=FunctionModel(work_fn)),
        agents.synthesize.override(model=FunctionModel(work_fn)),
    ):
        result = await _run(agents, "Research and summarize")

    assert isinstance(result, str)
    # 1 triage + 2 steps + 1 synthesize = 4
    assert call_count == 4
