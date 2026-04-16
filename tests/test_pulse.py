"""Tests for the Pulse model-loop planning pipeline."""

from __future__ import annotations

import json

import pytest
from pydantic_ai import Agent, ModelResponse, TextPart, ToolCallPart
from pydantic_ai.models.function import AgentInfo, FunctionModel
from pydantic_ai.tools import DeferredToolRequests

from octomate.tentacles.agent.pulse import PulseState
from octomate.tentacles.agent.pulse.tools import TodoInput, build_todo_list_toolset
from octomate.schemas.actions import AgentMessage
from octomate.schemas.segments import TextSegment
from octomate.schemas.session import SessionKey
from octomate.tentacles.agent.context import SessionContext
from octomate.transmuters.interactions import Todo


def _make_pulse_agent(model: FunctionModel | None = None) -> Agent:
    return Agent(
        model or "test",
        deps_type=SessionContext,
        output_type=[list[AgentMessage], DeferredToolRequests],
    )


def _mock_ctx() -> SessionContext:
    return SessionContext(
        session_key=SessionKey(tentacle_id="test", user_id="user-1"),
        tentacle=None,  # type: ignore[arg-type]
    )


def _msg_payload(text: str) -> str:
    return json.dumps(
        {"response": [{"segments": [{"type": "text", "data": {"text": text}}]}]}
    )


def _answer_response(text: str, info: AgentInfo) -> ModelResponse:
    """Return list[AgentMessage] output — direct answer path."""
    tool = next(
        (t for t in info.output_tools if "deferred" not in t.name.lower()),
        info.output_tools[0],
    )
    return ModelResponse(
        parts=[ToolCallPart(tool_name=tool.name, args=_msg_payload(text), tool_call_id="r")]
    )


def _tool_call_response(tool_name: str, args: dict, call_id: str = "t0") -> ModelResponse:
    """Return a regular tool call (not an output tool)."""
    return ModelResponse(
        parts=[ToolCallPart(tool_name=tool_name, args=json.dumps(args), tool_call_id=call_id)]
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


async def _run_process(agent: Agent, prompt: str | list, *, max_turns: int = 50) -> list[AgentMessage]:
    """Drive PulseTentacle.process() without a real model or channel."""
    from pydantic_ai.usage import UsageLimits

    state = PulseState(prompt=prompt)
    todo_toolset = build_todo_list_toolset(state)

    result = await agent.run(
        user_prompt=prompt if isinstance(prompt, str) else str(prompt),
        deps=_mock_ctx(),
        toolsets=[todo_toolset],
        usage_limits=UsageLimits(request_limit=max_turns),
    )
    return result.output


async def test_direct_answer_single_call():
    """Simple questions answered directly require only 1 LLM call."""
    call_count = 0

    def fn(messages: list, info: AgentInfo) -> ModelResponse:
        nonlocal call_count
        call_count += 1
        return _answer_response("42", info)

    agent = _make_pulse_agent(FunctionModel(fn))
    result = await _run_process(agent, "What is 6*7?")

    assert _extract_text(result) == "42"
    assert call_count == 1


async def test_todo_list_tool_updates_state():
    """Calling todo_list tool updates state.todos with the provided items."""
    state = PulseState(prompt="test")
    toolset = build_todo_list_toolset(state)

    agent = Agent("test", deps_type=SessionContext, output_type=[list[AgentMessage], DeferredToolRequests])

    call_count = 0

    def fn(messages: list, info: AgentInfo) -> ModelResponse:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return _tool_call_response(
                "todo_list",
                {
                    "todos": [
                        {"todo_id": "p-0", "title": "Research", "description": "Look it up"},
                        {"todo_id": "p-1", "title": "Write", "description": "Write the summary"},
                    ]
                },
            )
        return _answer_response("done", info)

    result = await agent.run(
        user_prompt="research and summarize",
        model=FunctionModel(fn),
        deps=_mock_ctx(),
        toolsets=[toolset],
    )

    assert len(state.todos) == 2
    assert all(isinstance(t, Todo) for t in state.todos)
    assert state.todos[0].title == "Research"
    assert state.todos[1].title == "Write"
    assert _extract_text(result.output) == "done"


async def test_todo_list_status_updates():
    """Re-calling todo_list with updated statuses updates state.todos accordingly."""
    state = PulseState(prompt="test")
    toolset = build_todo_list_toolset(state)
    agent = Agent("test", deps_type=SessionContext, output_type=[list[AgentMessage], DeferredToolRequests])

    call_count = 0

    def fn(messages: list, info: AgentInfo) -> ModelResponse:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return _tool_call_response(
                "todo_list",
                {"todos": [{"todo_id": "p-0", "title": "Step A", "status": "pending"}]},
            )
        if call_count == 2:
            return _tool_call_response(
                "todo_list",
                {"todos": [{"todo_id": "p-0", "title": "Step A", "status": "completed"}]},
            )
        return _answer_response("all done", info)

    await agent.run(user_prompt="do something", model=FunctionModel(fn), deps=_mock_ctx(), toolsets=[toolset])

    assert state.todos[0].status == "completed"


async def test_todo_list_tool_return_message():
    """todo_list tool returns a human-readable confirmation string."""
    state = PulseState(prompt="test")
    toolset = build_todo_list_toolset(state)
    agent = Agent("test", deps_type=SessionContext, output_type=[list[AgentMessage], DeferredToolRequests])

    confirmations: list[str] = []
    call_count = 0

    def fn(messages: list, info: AgentInfo) -> ModelResponse:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return _tool_call_response(
                "todo_list",
                {
                    "todos": [
                        {"todo_id": "p-0", "title": "A", "status": "in_progress"},
                        {"todo_id": "p-1", "title": "B", "status": "pending"},
                    ]
                },
            )
        # Capture the tool result from message history
        for msg in messages:
            content = str(msg)
            if "wrote 2 todos" in content:
                confirmations.append(content)
                break
        return _answer_response("ok", info)

    await agent.run(user_prompt="plan something", model=FunctionModel(fn), deps=_mock_ctx(), toolsets=[toolset])

    assert any("wrote 2 todos" in c for c in confirmations)


async def test_direct_answer_no_todo_tool_called():
    """Direct answers (greetings, simple questions) never call todo_list."""
    state = PulseState(prompt="hi")
    toolset = build_todo_list_toolset(state)
    agent = _make_pulse_agent(FunctionModel(lambda msgs, info: _answer_response("Hello!", info)))

    await agent.run(user_prompt="hi", deps=_mock_ctx(), toolsets=[toolset])

    assert state.todos == []


async def test_list_prompt():
    """Agent accepts a list prompt (multi-part content)."""
    agent = _make_pulse_agent(FunctionModel(lambda msgs, info: _answer_response("ok", info)))
    state = PulseState(prompt=["hello", "world"])
    result = await _run_process(agent, ["hello", "world"])
    assert _extract_text(result) == "ok"


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


async def test_todo_input_model_defaults():
    """TodoInput has sensible defaults for optional fields."""
    item = TodoInput(todo_id="p-0", title="My Step")
    assert item.status == "pending"
    assert item.description == ""
    assert item.depends_on == []
    assert item.active_form is None


async def test_multiple_todo_writes_preserve_server_ids():
    """Second todo_list call reuses server ids for existing todos (no double-create)."""
    from pydantic_ai.models.test import TestModel

    state = PulseState(prompt="test")
    create_calls: list[str] = []

    class MockTodoFeeler:
        async def create_todo(self, key, title, active_form=None, assignee=None):
            create_calls.append(title)
            return Todo(todo_id=f"server-{len(create_calls)}", title=title)

        async def update_todo(self, todo_id, status):
            return True

        async def upsert_todo_list(self, key, items, existing_ts=None):
            return "card-ref"

        async def pin_todo(self, key, card_ref):
            return True

        async def unpin_todo(self, key, card_ref):
            return True

    class MockFeelers:
        todos = MockTodoFeeler()

    class MockTentacle:
        feelers = MockFeelers()

    toolset = build_todo_list_toolset(state)
    mock_deps = SessionContext(
        session_key=SessionKey(tentacle_id="test", user_id="user-1"),
        tentacle=MockTentacle(),  # type: ignore[arg-type]
    )

    agent = Agent("test", deps_type=SessionContext, output_type=[list[AgentMessage], DeferredToolRequests])

    call_count = 0

    def fn(messages: list, info: AgentInfo) -> ModelResponse:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return _tool_call_response(
                "todo_list",
                {"todos": [{"todo_id": "p-0", "title": "Step A", "status": "pending"}]},
            )
        if call_count == 2:
            return _tool_call_response(
                "todo_list",
                {"todos": [{"todo_id": "p-0", "title": "Step A", "status": "completed"}]},
            )
        return _answer_response("done", info)

    await agent.run(
        user_prompt="test",
        deps=mock_deps,
        toolsets=[toolset],
        model=FunctionModel(fn),
    )

    # create_todo called only once — second call reuses the server id
    assert create_calls.count("Step A") == 1
