"""Unit tests for the Inkling ReAct loop with deferred-tool requests.

Drives the graph end-to-end against a scripted FunctionModel (no real LLM call)
to prove:
- `CallDeferred` from `ask_questions` leaves a deferred request for triage
- streaming events from `agent.run_stream_events` come from the graph output
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass, field
from typing import cast

import pytest

from pydantic_ai import Agent, AgentRunResult, AgentRunResultEvent
from pydantic_ai.messages import (
    AgentStreamEvent,
    ModelMessage,
    ModelResponse,
    TextPart,
    ToolCallPart,
)
from pydantic_ai.models.function import (
    AgentInfo,
    DeltaToolCall,
    DeltaToolCalls,
    FunctionModel,
)
from pydantic_ai.models.test import TestModel
from pydantic_ai.tools import DeferredToolRequests, DeferredToolResults
from pydantic_graph import End, Graph, GraphRunContext

from octomate import Octomate
from octomate.managers.conversations import ConversationManager
from octomate.schemas.conversation import Conversation, ConversationKey
from octomate.capabilities.react import (
    ReactDeps,
    ReactState,
    ResolveDeferred,
    ResumeTurn,
    RunAgent,
    StartTurn,
)
from octomate.config import ModelConfig
from octomate.providers import ProviderRegistry
from octomate.tentacles.agent.inkling import (
    InklingTentacle,
    build_inkling_agent,
    inkling_toolset,
)
from octomate.tentacles.agent.inkling.prompts import SYSTEM_PROMPT
from octomate.types.json import JsonObject

InklingTestOutput = str | DeferredToolRequests
InklingTestEvent = AgentStreamEvent | AgentRunResultEvent[InklingTestOutput]


class StubRegistry:
    """Returns a credential-free TestModel so build_inkling_agent runs without
    constructing a real provider client (Vertex/etc. would need credentials)."""

    def build_model(
        self, model: ModelConfig, settings: object | None = None
    ) -> TestModel:
        return TestModel()


def test_build_inkling_agent_passes_model_settings_through() -> None:
    agent = build_inkling_agent(
        cast(ProviderRegistry, StubRegistry()),
        ModelConfig(provider="vertex", name="gemini-3-flash-preview"),
        model_settings={"temperature": 0.2},
    )
    assert agent.name == "octomate-inkling"
    assert agent.model_settings == {"temperature": 0.2}


def test_build_inkling_agent_defaults_model_settings_to_none() -> None:
    # Per-model settings (incl. thinking) live in model.settings, applied by the
    # registry; the agent adds nothing of its own by default.
    agent = build_inkling_agent(
        cast(ProviderRegistry, StubRegistry()),
        ModelConfig(provider="vertex", name="gemini-3-flash-preview"),
    )
    assert agent.model_settings is None


@dataclass
class ScriptedTurn:
    """One turn of the conversation: one tool call to emit as a streamed delta."""

    tool_name: str
    args: JsonObject
    tool_call_id: str


@dataclass
class ScriptedStream:
    """FunctionModel stream callback: emits tool-call deltas or text output."""

    turns: list[ScriptedTurn | str]
    cursor: int = 0
    seen_output_tools: list[list[str]] = field(default_factory=list)
    __name__: str = "scripted_stream"

    def __call__(
        self, messages: list[ModelMessage], info: AgentInfo
    ) -> AsyncIterator[str | DeltaToolCalls]:
        self.seen_output_tools.append([t.name for t in info.output_tools])
        turn = self.turns[self.cursor]
        self.cursor += 1
        return _emit_scripted_turn(turn)


@dataclass
class _FakeConversation:
    """Stand-in whose `messages` is a plain list react can read + accumulate
    into, since the real arcanus relation can't be appended to detached."""

    id: str = "fake-conversation"
    messages: list[ModelMessage] = field(default_factory=list)


@dataclass
class FakeConversationManager(ConversationManager):
    conversation: _FakeConversation = field(default_factory=_FakeConversation)
    runs: list[tuple[Conversation, str, list[ModelMessage]]] = field(
        default_factory=list
    )

    async def ensure(
        self,
        key: ConversationKey,
        *,
        agent_tentacle_id: str,
    ) -> Conversation:
        return cast(Conversation, self.conversation)

    async def record_agent_run(
        self,
        conversation: Conversation,
        run_id: str,
        messages: Sequence[ModelMessage],
        *,
        name: str | None = None,
    ) -> None:
        self.runs.append((conversation, f"{name}:{run_id}", list(messages)))
        self.conversation.messages.extend(messages)

    async def drop_trailing_deferral(
        self,
        conversation: Conversation,
    ) -> None:
        return None


async def _emit_scripted_turn(
    turn: ScriptedTurn | str,
) -> AsyncIterator[str | DeltaToolCalls]:
    if isinstance(turn, str):
        yield turn
        return
    yield {
        0: DeltaToolCall(
            name=turn.tool_name,
            json_args=json.dumps(turn.args),
            tool_call_id=turn.tool_call_id,
        )
    }


def _build_test_agent(
    turns: list[ScriptedTurn | str],
) -> tuple[Agent[None, InklingTestOutput], ScriptedStream]:
    script = ScriptedStream(turns=turns)
    agent: Agent[None, InklingTestOutput] = Agent(
        FunctionModel(stream_function=script, model_name="scripted"),
        deps_type=type(None),
        output_type=[str, DeferredToolRequests],
        toolsets=[inkling_toolset],
        system_prompt=SYSTEM_PROMPT,
    )
    return agent, script


def _build_non_stream_agent() -> Agent[None, InklingTestOutput]:
    def respond(
        messages: list[ModelMessage],
        info: AgentInfo,
    ) -> ModelResponse:
        return ModelResponse(parts=[TextPart(content="all done!")])

    return Agent(
        FunctionModel(function=respond, model_name="scripted"),
        deps_type=type(None),
        output_type=[str, DeferredToolRequests],
        toolsets=[inkling_toolset],
        system_prompt=SYSTEM_PROMPT,
    )


def _test_conversation_key() -> ConversationKey:
    return ConversationKey(
        channel_tentacle_id="test",
        chat_type="private",
        chat_id="test",
        user_id="test",
    )


async def test_inkling_loop_emits_deferred_question_batch() -> None:
    agent, script = _build_test_agent(
        [
            ScriptedTurn(
                tool_name="ask_questions",
                args={
                    "questions": [
                        {
                            "question": "what's your name?",
                            "choices": ["Ada", "Grace"],
                            "hint": "Pick or type the name to use.",
                        },
                    ]
                },
                tool_call_id="call_ask_1",
            ),
        ]
    )

    captured_events: list[InklingTestEvent] = []
    conversations = FakeConversationManager()
    tentacle = InklingTentacle(
        "inkling",
        Octomate(conversations=conversations),
        agent=agent,
        conversation_manager=conversations,
    )
    async with tentacle.run_stream_events(
        "hi octomate",
        conversation_key=_test_conversation_key(),
    ) as stream:
        async for event in stream:
            captured_events.append(event)

    result_events = [
        event for event in captured_events if isinstance(event, AgentRunResultEvent)
    ]
    assert len(result_events) == 1

    output = result_events[-1].result.output
    assert isinstance(output, DeferredToolRequests)
    assert len(output.calls) == 1
    call = output.calls[0]
    assert call.tool_name == "ask_questions"
    assert call.args_as_dict()["questions"][0]["question"] == "what's your name?"

    assert captured_events, "graph output should stream pydantic events"

    assert script.cursor == 1
    assert len(conversations.runs) == 1
    assert all(run[1].startswith("react:") for run in conversations.runs)


async def test_inkling_tentacle_invokes_suspender_on_deferred_request() -> None:
    """The real InklingTentacle -> react graph must invoke a supplied suspender
    when the run yields DeferredToolRequests (the persist+present contract)."""

    agent, _ = _build_test_agent(
        [
            ScriptedTurn(
                tool_name="ask_questions",
                args={"questions": [{"question": "what's your name?"}]},
                tool_call_id="call_ask_1",
            ),
        ]
    )
    conversations = FakeConversationManager()
    tentacle = InklingTentacle(
        "inkling",
        Octomate(conversations=conversations),
        agent=agent,
        conversation_manager=conversations,
    )
    suspender = _StubSuspender()

    outputs: list[object] = []
    async with tentacle.run_stream_events(
        "hi octomate",
        conversation_key=_test_conversation_key(),
        deferred_suspender=suspender,
    ) as stream:
        async for event in stream:
            if isinstance(event, AgentRunResultEvent):
                outputs.append(event.result.output)

    assert len(suspender.suspended) == 1
    assert isinstance(suspender.suspended[0], DeferredToolRequests)
    assert suspender.suspended[0] is outputs[-1]


async def test_inkling_tentacle_stream_events_forwards_graph_events() -> None:
    agent, script = _build_test_agent(
        [
            "all done!",
        ]
    )
    conversations = FakeConversationManager()
    tentacle = InklingTentacle(
        "inkling",
        Octomate(conversations=conversations),
        agent=agent,
        conversation_manager=conversations,
    )

    captured_events: list[InklingTestEvent] = []
    async with tentacle.run_stream_events(
        "hi octomate",
        conversation_key=_test_conversation_key(),
    ) as stream:
        async for event in stream:
            captured_events.append(event)

    result_events = [
        event for event in captured_events if isinstance(event, AgentRunResultEvent)
    ]
    assert result_events
    assert result_events[-1].result.output == "all done!"
    assert script.cursor == 1


async def test_inkling_loop_propagates_graph_error_streaming() -> None:
    """A model/graph error during a streamed run must surface to the caller
    rather than be swallowed by the background graph task (which would otherwise
    cancel the consumer mid-event and mask the real error)."""

    async def boom(
        messages: list[ModelMessage], info: AgentInfo
    ) -> AsyncIterator[str]:
        raise RuntimeError("model boom")
        yield ""  # pragma: no cover - marks this an async generator

    agent: Agent[None, InklingTestOutput] = Agent(
        FunctionModel(stream_function=boom, model_name="scripted"),
        deps_type=type(None),
        output_type=[str, DeferredToolRequests],
        toolsets=[inkling_toolset],
        system_prompt=SYSTEM_PROMPT,
    )
    conversations = FakeConversationManager()
    tentacle = InklingTentacle(
        "inkling",
        Octomate(conversations=conversations),
        agent=agent,
        conversation_manager=conversations,
    )

    with pytest.raises(RuntimeError, match="model boom"):
        async with tentacle.run_stream_events(
            "hi octomate",
            conversation_key=_test_conversation_key(),
        ) as stream:
            async for _ in stream:
                pass


async def test_inkling_loop_propagates_graph_error_collected_run() -> None:
    """`run` collects graph events internally; a graph error must still surface
    to the caller rather than be lost in the background task."""

    async def boom(
        messages: list[ModelMessage], info: AgentInfo
    ) -> AsyncIterator[str]:
        raise RuntimeError("model boom")
        yield ""  # pragma: no cover - marks this an async generator

    agent: Agent[None, InklingTestOutput] = Agent(
        FunctionModel(stream_function=boom, model_name="scripted"),
        deps_type=type(None),
        output_type=[str, DeferredToolRequests],
        toolsets=[inkling_toolset],
        system_prompt=SYSTEM_PROMPT,
    )
    conversations = FakeConversationManager()
    tentacle = InklingTentacle(
        "inkling",
        Octomate(conversations=conversations),
        agent=agent,
        conversation_manager=conversations,
    )

    with pytest.raises(RuntimeError, match="model boom"):
        await tentacle.run(
            "hi octomate",
            conversation_key=_test_conversation_key(),
        )


async def test_inkling_loop_handles_immediate_final_response() -> None:
    """If the model finalizes immediately, the loop ends after one RunAgent step."""

    agent = _build_non_stream_agent()

    deps = ReactDeps[InklingTestOutput, None](
        agent=agent,
        conversation_manager=FakeConversationManager(),
        agent_deps=None,
    )
    graph: Graph[
        ReactState,
        ReactDeps[InklingTestOutput, None],
        AgentRunResult[InklingTestOutput],
    ] = Graph(
        nodes=[StartTurn, ResumeTurn, RunAgent, ResolveDeferred],
        name="react",
    )

    result = await graph.run(
        StartTurn(user_prompt="just say done"),
        state=ReactState(
            conversation_key=_test_conversation_key(), agent_tentacle_id="inkling"
        ),
        deps=deps,
    )

    assert result.output.output == "all done!"


@dataclass
class _StubResolver:
    results: DeferredToolResults
    calls: int = 0

    async def resolve(self, requests: DeferredToolRequests) -> DeferredToolResults:
        self.calls += 1
        return self.results


@dataclass
class _StubSuspender:
    suspended: list[DeferredToolRequests] = field(default_factory=list)

    async def suspend(self, requests: DeferredToolRequests) -> None:
        self.suspended.append(requests)


def _deferred_requests() -> DeferredToolRequests:
    return DeferredToolRequests(
        calls=[
            ToolCallPart(
                tool_name="ask_questions",
                args={"questions": [{"question": "?"}]},
                tool_call_id="call_ask_1",
            )
        ]
    )


def _react_deps(
    *,
    resolver: object | None = None,
    suspender: object | None = None,
) -> ReactDeps[InklingTestOutput, None]:
    return ReactDeps(
        agent=cast("Agent[None, InklingTestOutput]", object()),
        conversation_manager=FakeConversationManager(),
        agent_deps=None,
        resolver=cast("None", resolver),
        suspender=cast("None", suspender),
    )


def _ctx(
    deps: ReactDeps[InklingTestOutput, None],
) -> GraphRunContext[ReactState, ReactDeps[InklingTestOutput, None]]:
    return GraphRunContext(
        state=ReactState(
            conversation_key=_test_conversation_key(), agent_tentacle_id="inkling"
        ),
        deps=deps,
    )


async def test_resolve_deferred_resolves_in_process_when_resolver_set() -> None:
    requests = _deferred_requests()
    results = DeferredToolResults()
    results.calls["call_ask_1"] = ["Ada"]
    resolver = _StubResolver(results)

    node: ResolveDeferred[InklingTestOutput, None] = ResolveDeferred(
        requests=requests, result=AgentRunResult(requests)
    )
    nxt = await node.run(_ctx(_react_deps(resolver=resolver)))

    assert isinstance(nxt, RunAgent)
    assert nxt.deferred_results is results
    assert resolver.calls == 1


async def test_resolve_deferred_suspends_when_only_suspender_set() -> None:
    requests = _deferred_requests()
    suspender = _StubSuspender()
    run_result = AgentRunResult(requests)

    node: ResolveDeferred[InklingTestOutput, None] = ResolveDeferred(
        requests=requests, result=run_result
    )
    nxt = await node.run(_ctx(_react_deps(suspender=suspender)))

    assert isinstance(nxt, End)
    assert nxt.data is run_result
    assert suspender.suspended == [requests]


async def test_resolve_deferred_requires_a_hook() -> None:
    requests = _deferred_requests()
    node: ResolveDeferred[InklingTestOutput, None] = ResolveDeferred(
        requests=requests, result=AgentRunResult(requests)
    )
    with pytest.raises(RuntimeError, match="resolver or suspender"):
        await node.run(_ctx(_react_deps()))
