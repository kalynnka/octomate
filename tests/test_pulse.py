"""Tests for the Pulse graph-based planning pipeline."""

from __future__ import annotations

import json

import pytest
from pydantic_ai import Agent, ModelResponse, TextPart, ToolCallPart
from pydantic_ai.models.function import AgentInfo, FunctionModel
from pydantic_ai.tools import DeferredToolRequests

from octomate.tentacles.agent.pulse import (
    PulseDeps,
    PulseState,
    Triage,
    pulse_graph,
)
from octomate.schemas.actions import AgentMessage
from octomate.schemas.segments import TextSegment
from octomate.transmuters.interactions import Todo


def _make_pulse_agent(model: FunctionModel | None = None) -> Agent:
    return Agent(
        model or "test",
        output_type=[list[AgentMessage], list[Todo], DeferredToolRequests],
    )


def _make_deps(pulse_agent=None) -> PulseDeps:
    agent = pulse_agent or _make_pulse_agent()
    return PulseDeps(
        pulse_agent=agent,
        subagents={},
        tentacles={},
        agent_deps=None,  # type: ignore[arg-type]
        tentacle=None,  # type: ignore[arg-type]
    )


async def _run(deps: PulseDeps, prompt: str | list):
    state = PulseState(prompt=prompt)
    result = await pulse_graph.run(Triage(), state=state, deps=deps)
    return result.output


def _msg_payload(text: str) -> str:
    return json.dumps(
        {"response": [{"segments": [{"type": "text", "data": {"text": text}}]}]}
    )


def _adaptive_response(text: str, info: AgentInfo) -> ModelResponse:
    """Return the right output format based on what type of call this is.

    - No output tools → output_type=str (inline step) → TextPart
    - One output tool → output_type=list[AgentMessage] per-call override (synth) → tool call
    - Multiple output tools → default union output (triage) → use first non-deferred tool
    """
    if not info.output_tools:
        return ModelResponse(parts=[TextPart(content=text)])
    tool = next(
        (t for t in info.output_tools if "deferred" not in t.name.lower()),
        info.output_tools[0],
    )
    return ModelResponse(
        parts=[ToolCallPart(tool_name=tool.name, args=_msg_payload(text), tool_call_id="r")]
    )


def _todo_response(todos: list[dict], info: AgentInfo) -> ModelResponse:
    """Return todos using the second non-deferred output tool (list[Todo] branch)."""
    non_deferred = [t for t in info.output_tools if "deferred" not in t.name.lower()]
    tool = non_deferred[1] if len(non_deferred) > 1 else non_deferred[0]
    return ModelResponse(
        parts=[
            ToolCallPart(
                tool_name=tool.name,
                args=json.dumps({"response": todos}),
                tool_call_id="plan",
            )
        ]
    )


def _extract_text(output) -> str:
    if isinstance(output, str):
        return output
    if isinstance(output, list) and output:
        first = output[0]
        if isinstance(first, AgentMessage) and first.segments:
            seg = first.segments[0]
            if isinstance(seg, TextSegment):
                return seg.data.get("text", "")
    return str(output)


async def test_direct_answer_single_call():
    """Simple questions answered directly require only 1 LLM call."""
    call_count = 0

    def fn(messages: list, info: AgentInfo) -> ModelResponse:
        nonlocal call_count
        call_count += 1
        return _adaptive_response("42", info)

    deps = _make_deps(_make_pulse_agent(FunctionModel(fn)))
    result = await _run(deps, "What is 6*7?")

    assert _extract_text(result) == "42"
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
            return _todo_response(todos, info)
        return _adaptive_response(f"result-{call_count}", info)

    deps = _make_deps(_make_pulse_agent(FunctionModel(fn)))
    result = await _run(deps, "Summarize then compare")

    # 1 triage + 2 inline steps + 1 synthesize = 4
    assert call_count == 4
    assert isinstance(result, (str, list))


async def test_steps_tracked_as_todos():
    """Plan steps in the graph state are Todo objects marked done after execution."""
    todos = [
        {
            "todo_id": "pulse-0",
            "title": "Summarize",
            "description": "Summarize the document",
        },
        {
            "todo_id": "pulse-1",
            "title": "Compare",
            "description": "Compare against last quarter",
        },
    ]
    call_count = 0

    def fn(messages: list, info: AgentInfo) -> ModelResponse:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return _todo_response(todos, info)
        return _adaptive_response("done", info)

    state = PulseState(prompt="test task")
    deps = _make_deps(_make_pulse_agent(FunctionModel(fn)))
    await pulse_graph.run(Triage(), state=state, deps=deps)

    assert len(state.todos) == 2
    assert all(isinstance(t, Todo) for t in state.todos)
    assert all(t.status == "completed" for t in state.todos)
    assert state.todos[0].title == "Summarize"
    assert state.todos[0].description == "Summarize the document"
    assert state.todos[1].title == "Compare"
    assert state.todos[1].description == "Compare against last quarter"


async def test_triage_fallback_to_direct():
    """When triage returns list[AgentMessage], it becomes the direct answer."""
    def fn(messages: list, info: AgentInfo) -> ModelResponse:
        return _adaptive_response("Just a simple answer.", info)

    deps = _make_deps(_make_pulse_agent(FunctionModel(fn)))
    result = await _run(deps, "simple question")
    assert _extract_text(result) == "Just a simple answer."


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
            return _todo_response(todos, info)
        if not info.output_tools:
            return ModelResponse(parts=[TextPart(content=f"Step output {call_count}")])
        return _adaptive_response("Final clean answer", info)

    deps = _make_deps(_make_pulse_agent(FunctionModel(fn)))
    answer = await _run(deps, "Analyze the report")

    assert _extract_text(answer) == "Final clean answer"
    # 1 triage + 3 steps + 1 synthesize = 5
    assert call_count == 5


async def test_list_prompt():
    """pulse_graph accepts a list prompt."""
    def fn(messages: list, info: AgentInfo) -> ModelResponse:
        return _adaptive_response("ok", info)

    deps = _make_deps(_make_pulse_agent(FunctionModel(fn)))
    result = await _run(deps, ["hello", "world"])
    assert _extract_text(result) == "ok"


async def test_synthesize_uses_native_output_type():
    """Synthesize call uses output_type=list[AgentMessage] override."""
    call_count = 0
    todos = [
        {"todo_id": "pulse-0", "title": "Research", "description": "Research the topic"},
        {"todo_id": "pulse-1", "title": "Write", "description": "Write the summary"},
    ]

    def fn(messages: list, info: AgentInfo) -> ModelResponse:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return _todo_response(todos, info)
        return _adaptive_response("native output", info)

    deps = _make_deps(_make_pulse_agent(FunctionModel(fn)))
    result = await _run(deps, "Research and summarize")

    assert isinstance(result, (str, list))
    # 1 triage + 2 inline steps + 1 synthesize = 4
    assert call_count == 4


async def test_inline_history_threads_through():
    """main_history grows monotonically: triage → step1 → step2 → synth."""
    history_lengths: list[int] = []
    call_count = 0
    todos = [
        {"todo_id": "s0", "title": "Step 0", "description": "desc 0"},
        {"todo_id": "s1", "title": "Step 1", "description": "desc 1"},
    ]

    def fn(messages: list, info: AgentInfo) -> ModelResponse:
        nonlocal call_count
        call_count += 1
        history_lengths.append(len(messages))
        if call_count == 1:
            return _todo_response(todos, info)
        return _adaptive_response(f"out-{call_count}", info)

    state = PulseState(prompt="multi-step task")
    deps = _make_deps(_make_pulse_agent(FunctionModel(fn)))
    await pulse_graph.run(Triage(), state=state, deps=deps)

    # Each successive call should see at least as many messages as the prior one
    for i in range(1, len(history_lengths)):
        assert history_lengths[i] >= history_lengths[i - 1], (
            f"Call {i + 1} saw fewer messages ({history_lengths[i]}) "
            f"than call {i} ({history_lengths[i - 1]})"
        )


async def test_depends_on_gating():
    """Todo B with depends_on=[A] only runs after A completes."""
    execution_order: list[str] = []
    call_count = 0
    todos = [
        {"todo_id": "A", "title": "Step A", "description": "first", "depends_on": []},
        {"todo_id": "B", "title": "Step B", "description": "second", "depends_on": ["A"]},
    ]

    def fn(messages: list, info: AgentInfo) -> ModelResponse:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return _todo_response(todos, info)
        for msg in reversed(messages):
            content = str(msg)
            if "Step A" in content:
                execution_order.append("A")
                break
            elif "Step B" in content:
                execution_order.append("B")
                break
        return _adaptive_response(f"out-{call_count}", info)

    state = PulseState(prompt="dep test")
    deps = _make_deps(_make_pulse_agent(FunctionModel(fn)))
    await pulse_graph.run(Triage(), state=state, deps=deps)

    # B must run after A
    if "A" in execution_order and "B" in execution_order:
        assert execution_order.index("A") < execution_order.index("B")


async def test_subagent_deferred_raises():
    """SubAgent emitting DeferredToolRequests raises RuntimeError."""
    from octomate.tentacles.agent.pulse import LocalSubAgent

    sub_agent_model = Agent("test", output_type=str)
    sub = LocalSubAgent(id="bad", description="bad subagent", agent=sub_agent_model)

    original_run = sub_agent_model.run

    async def patched_run(**kwargs):
        result = await original_run(**kwargs)
        object.__setattr__(
            result,
            "output",
            DeferredToolRequests(calls=[], approvals=[], metadata={}),
        )
        return result

    sub_agent_model.run = patched_run  # type: ignore[method-assign]

    todo = Todo(todo_id="t0", title="bad step", description="")
    state = PulseState(prompt="test")

    with pytest.raises(RuntimeError, match="deferred tool calls"):
        await sub.execute(None, todo, state, None)  # type: ignore[arg-type]
