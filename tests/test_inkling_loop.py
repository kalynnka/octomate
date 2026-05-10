"""Unit tests for the Inkling ReAct loop with deferred-tool resolution.

Drives the graph end-to-end against a scripted FunctionModel (no real LLM call)
to prove:
- both deferral flavors (`CallDeferred` from `ask_user`, `requires_approval=True`
  from `send_email`) round-trip through the loop
- streaming events from `agent.run_stream_events` reach the deps event_sink
- `state.message_history` accumulates across iterations
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

from pydantic_ai import Agent
from pydantic_ai.messages import (
    AgentStreamEvent,
    ModelMessage,
    ToolCallPart,
)
from pydantic_ai.models.function import (
    AgentInfo,
    DeltaToolCall,
    DeltaToolCalls,
    FunctionModel,
)
from pydantic_ai.tools import DeferredToolRequests

from octomate.schemas.actions import AgentMessage
from octomate.schemas.segments import TextSegment
from octomate.tentacles.agent.inkling import (
    InklingDeps,
    InklingState,
    RunAgent,
    StubResolver,
    inkling_graph,
    inkling_toolset,
)
from octomate.tentacles.agent.inkling.prompts import SYSTEM_PROMPT


@dataclass
class ScriptedTurn:
    """One turn of the conversation: one tool call to emit as a streamed delta."""

    tool_name: str
    args: dict[str, Any]
    tool_call_id: str


@dataclass
class ScriptedStream:
    """FunctionModel stream callback: emits each scripted turn as a single delta."""

    turns: list[ScriptedTurn]
    cursor: int = 0
    seen_output_tools: list[list[str]] = field(default_factory=list)
    __name__: str = "scripted_stream"

    def __call__(
        self, messages: list[ModelMessage], info: AgentInfo
    ) -> AsyncIterator[DeltaToolCalls]:
        self.seen_output_tools.append([t.name for t in info.output_tools])
        turn = self.turns[self.cursor]
        self.cursor += 1
        return _emit_single_tool_call(turn)


async def _emit_single_tool_call(
    turn: ScriptedTurn,
) -> AsyncIterator[DeltaToolCalls]:
    yield {
        0: DeltaToolCall(
            name=turn.tool_name,
            json_args=json.dumps(turn.args),
            tool_call_id=turn.tool_call_id,
        )
    }


def _final_result_args() -> dict[str, Any]:
    """Args for the synthetic `final_result` output tool wrapping list[AgentMessage].

    pydantic-ai wraps non-model-like output types as `{"response": <value>}`.
    """
    return {
        "response": [
            AgentMessage(
                segments=[TextSegment(data={"text": "all done!"})]
            ).model_dump()
        ]
    }


def _build_test_agent(
    turns: list[ScriptedTurn],
) -> tuple[Agent[None, Any], ScriptedStream]:
    script = ScriptedStream(turns=turns)
    agent: Agent[None, Any] = Agent(
        FunctionModel(stream_function=script, model_name="scripted"),
        deps_type=type(None),
        output_type=[list[AgentMessage], DeferredToolRequests],
        toolsets=[inkling_toolset],
        system_prompt=SYSTEM_PROMPT,
    )
    return agent, script


async def test_inkling_loop_resolves_both_deferral_flavors() -> None:
    agent, script = _build_test_agent(
        [
            ScriptedTurn(
                tool_name="ask_user",
                args={"question": "what's your name?"},
                tool_call_id="call_ask_1",
            ),
            ScriptedTurn(
                tool_name="send_email",
                args={
                    "to": "alice@example.com",
                    "subject": "hi",
                    "body": "hello",
                },
                tool_call_id="call_email_1",
            ),
            ScriptedTurn(
                tool_name="final_result",
                args=_final_result_args(),
                tool_call_id="call_final_1",
            ),
        ]
    )

    visited: list[str] = []
    captured_events: list[AgentStreamEvent] = []

    async def sink(event: AgentStreamEvent) -> None:
        captured_events.append(event)

    deps = InklingDeps(
        agent=agent,
        resolver=StubResolver(canned={"ask_user": "Alice"}),
        user_prompt="hi octomate",
        event_sink=sink,
    )
    state = InklingState()

    async with inkling_graph.iter(RunAgent(), state=state, deps=deps) as run:
        async for node in run:
            visited.append(type(node).__name__)
        result = run.result

    assert result is not None
    assert visited == [
        "RunAgent",
        "ResolveDeferred",
        "RunAgent",
        "ResolveDeferred",
        "RunAgent",
        "End",
    ]

    output = result.output
    assert isinstance(output, list)
    assert len(output) == 1
    assert isinstance(output[0], AgentMessage)
    assert str(output[0]) == "all done!"

    assert state.message_history, "message history should accumulate across turns"

    assert captured_events, "event_sink should receive stream events"

    assert script.cursor == 3, "all three turns should have been consumed"


async def test_inkling_loop_handles_immediate_final_response() -> None:
    """If the model finalizes immediately, the loop ends after one RunAgent step."""

    agent, _ = _build_test_agent(
        [
            ScriptedTurn(
                tool_name="final_result",
                args=_final_result_args(),
                tool_call_id="call_final_immediate",
            )
        ]
    )

    deps = InklingDeps(
        agent=agent,
        resolver=StubResolver(),
        user_prompt="just say done",
    )

    visited: list[str] = []
    async with inkling_graph.iter(RunAgent(), state=InklingState(), deps=deps) as run:
        async for node in run:
            visited.append(type(node).__name__)

    assert visited == ["RunAgent", "End"]


async def test_stub_resolver_round_trips_calls_and_approvals() -> None:
    """Direct unit-style check on StubResolver — calls get canned strings, approvals get bools."""

    requests = DeferredToolRequests(
        calls=[
            ToolCallPart(
                tool_name="ask_user", args={"question": "?"}, tool_call_id="c1"
            )
        ],
        approvals=[
            ToolCallPart(
                tool_name="send_email",
                args={"to": "x", "subject": "y", "body": "z"},
                tool_call_id="a1",
            )
        ],
    )
    resolver = StubResolver(canned={"ask_user": "yes please"})
    results = await resolver.resolve(requests)

    assert results.calls["c1"] == "yes please"
    assert results.approvals["a1"] is True
