"""The react run-loop graph: StartTurn, ResumeTurn, RunAgent, ResolveDeferred,
and the `iter_react_graph_events` streaming bridge — no real LLM calls."""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import cast

import anyio
import pytest
from pydantic_ai import AgentRunResult, AgentRunResultEvent
from pydantic_ai.messages import ModelMessage, ToolCallPart
from pydantic_ai.models.function import AgentInfo, FunctionModel
from pydantic_ai.tools import DeferredToolRequests, DeferredToolResults
from pydantic_graph import End, Graph, GraphRunContext

from octomate.capabilities.agent import Agent
from octomate.capabilities.events import ActionBatchEvent
from octomate.capabilities.react import (
    REACT_NODES,
    ReactDeps,
    ReactState,
    ReactStreamEvent,
    ResolveDeferred,
    ResumeTurn,
    RunAgent,
    StartTurn,
    iter_react_graph_events,
)
from octomate.schemas.conversation import Conversation, ConversationKey
from octomate.tentacles.agent.inkling import inkling_toolset
from octomate.tentacles.agent.inkling.prompts import SYSTEM_PROMPT

from tests.support.agents import (
    ScriptedOutput,
    build_non_stream_agent,
    build_scripted_agent,
)
from tests.support.managers import FakeConversationManager


@dataclass
class StubResolver:
    results: DeferredToolResults
    calls: int = 0

    async def resolve(self, requests: DeferredToolRequests) -> DeferredToolResults:
        self.calls += 1
        return self.results


@dataclass
class StubSuspender:
    event: ActionBatchEvent | None = None
    suspended: list[DeferredToolRequests] = field(default_factory=list)

    async def suspend(
        self, requests: DeferredToolRequests
    ) -> ActionBatchEvent | None:
        self.suspended.append(requests)
        return self.event


def _key() -> ConversationKey:
    return ConversationKey(
        channel_tentacle_id="test",
        chat_type="private",
        chat_id="test",
        user_id="test",
    )


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


def _deps(
    *,
    agent: Agent[None, ScriptedOutput] | None = None,
    conversations: FakeConversationManager | None = None,
    resolver: StubResolver | None = None,
    suspender: StubSuspender | None = None,
    run_name: str = "react",
) -> ReactDeps[ScriptedOutput, None]:
    return ReactDeps(
        agent=agent or cast("Agent[None, ScriptedOutput]", object()),
        conversation_manager=conversations or FakeConversationManager(),
        agent_deps=None,
        resolver=resolver,
        suspender=suspender,
        run_name=run_name,
    )


def _ctx(
    deps: ReactDeps[ScriptedOutput, None],
) -> GraphRunContext[ReactState, ReactDeps[ScriptedOutput, None]]:
    return GraphRunContext(
        state=ReactState(conversation_key=_key(), agent_tentacle_id="inkling"),
        deps=deps,
    )


def _graph() -> Graph[
    ReactState,
    ReactDeps[ScriptedOutput, None],
    AgentRunResult[ScriptedOutput],
]:
    return Graph(nodes=REACT_NODES, name="react")


async def test_start_turn_drops_trailing_deferral_on_new_prompt() -> None:
    @dataclass
    class DropRecorder(FakeConversationManager):
        dropped: list[Conversation] = field(default_factory=list)

        async def drop_trailing_deferral(
            self,
            conversation: Conversation,
        ) -> None:
            self.dropped.append(conversation)
            return None

    recorder = DropRecorder()
    node: StartTurn[ScriptedOutput, None] = StartTurn(user_prompt="hi")

    nxt = await node.run(_ctx(_deps(conversations=recorder)))

    assert isinstance(nxt, RunAgent)
    assert nxt.user_prompt == "hi"
    assert [key for key, _ in recorder.ensured] == [_key()]
    assert len(recorder.dropped) == 1

    # Without a fresh prompt there is nothing to supersede: no conversation IO.
    silent = DropRecorder()
    nxt = await StartTurn[ScriptedOutput, None](user_prompt=None).run(
        _ctx(_deps(conversations=silent))
    )
    assert isinstance(nxt, RunAgent)
    assert silent.ensured == []
    assert silent.dropped == []


async def test_resume_turn_requires_resolved_calls_or_approvals() -> None:
    node: ResumeTurn[ScriptedOutput, None] = ResumeTurn(
        deferred_results=DeferredToolResults()
    )
    with pytest.raises(ValueError, match="at least one resolved call or approval"):
        await node.run(_ctx(_deps()))

    results = DeferredToolResults()
    results.calls["call_ask_1"] = ["Ada"]
    nxt = await ResumeTurn[ScriptedOutput, None](deferred_results=results).run(
        _ctx(_deps())
    )
    assert isinstance(nxt, RunAgent)
    assert nxt.deferred_results is results


async def test_resolve_deferred_resolves_in_process_when_resolver_set() -> None:
    requests = _deferred_requests()
    results = DeferredToolResults()
    results.calls["call_ask_1"] = ["Ada"]
    resolver = StubResolver(results)

    node: ResolveDeferred[ScriptedOutput, None] = ResolveDeferred(
        requests=requests, result=AgentRunResult(requests)
    )
    nxt = await node.run(_ctx(_deps(resolver=resolver)))

    assert isinstance(nxt, RunAgent)
    assert nxt.deferred_results is results
    assert resolver.calls == 1


async def test_resolve_deferred_suspends_when_only_suspender_set() -> None:
    requests = _deferred_requests()
    suspender = StubSuspender()
    run_result: AgentRunResult[ScriptedOutput] = AgentRunResult(requests)

    node: ResolveDeferred[ScriptedOutput, None] = ResolveDeferred(
        requests=requests, result=run_result
    )
    nxt = await node.run(_ctx(_deps(suspender=suspender)))

    assert isinstance(nxt, End)
    assert nxt.data is run_result
    assert suspender.suspended == [requests]


async def test_resolve_deferred_requires_a_hook() -> None:
    requests = _deferred_requests()
    node: ResolveDeferred[ScriptedOutput, None] = ResolveDeferred(
        requests=requests, result=AgentRunResult(requests)
    )
    with pytest.raises(RuntimeError, match="resolver or suspender"):
        await node.run(_ctx(_deps()))


async def test_suspender_event_is_forwarded_on_stream() -> None:
    requests = _deferred_requests()
    batch_event = ActionBatchEvent(batch_id="b1")
    suspender = StubSuspender(event=batch_event)
    send, receive = anyio.create_memory_object_stream[
        ReactStreamEvent[ScriptedOutput]
    ](10)
    deps = _deps(suspender=suspender)
    deps.event_send_stream = send

    node: ResolveDeferred[ScriptedOutput, None] = ResolveDeferred(
        requests=requests, result=AgentRunResult(requests)
    )
    nxt = await node.run(_ctx(deps))
    await send.aclose()

    assert isinstance(nxt, End)
    events = [event async for event in receive]
    assert events == [batch_event]


async def test_graph_ends_after_immediate_final_response() -> None:
    """If the model finalizes immediately, the loop ends after one RunAgent step."""
    agent = build_non_stream_agent()
    deps = _deps(agent=agent)

    result = await _graph().run(
        StartTurn(user_prompt="just say done"),
        state=ReactState(conversation_key=_key(), agent_tentacle_id="inkling"),
        deps=deps,
    )

    assert result.output.output == "all done!"


async def test_run_agent_records_new_messages_with_run_name() -> None:
    agent = build_non_stream_agent()
    conversations = FakeConversationManager()
    deps = _deps(agent=agent, conversations=conversations, run_name="custom")

    await _graph().run(
        StartTurn(user_prompt="just say done"),
        state=ReactState(conversation_key=_key(), agent_tentacle_id="inkling"),
        deps=deps,
    )

    assert len(conversations.runs) == 1
    _, run_label, messages = conversations.runs[0]
    assert run_label.startswith("custom:")
    assert messages
    # The recorded turn lands in the conversation the next ensure() serves.
    assert conversations.store[_key()].messages == messages


async def test_run_agent_streaming_sends_every_event_and_requires_result() -> None:
    agent, _ = build_scripted_agent(["all done!"])
    send, receive = anyio.create_memory_object_stream[
        ReactStreamEvent[ScriptedOutput]
    ](100)
    deps = _deps(agent=agent)
    deps.event_send_stream = send

    node: RunAgent[ScriptedOutput, None] = RunAgent(user_prompt="hi")
    nxt = await node.run(_ctx(deps))
    await send.aclose()

    assert isinstance(nxt, End)
    events = [event async for event in receive]
    assert events, "the streaming path must forward the run's events"
    assert isinstance(events[-1], AgentRunResultEvent)
    assert events[-1].result.output == "all done!"

    # A stream that never yields the terminal result event is a broken contract.
    class SilentAgent:
        def stream_events(
            self, *args: object, **kwargs: object
        ) -> AsyncIterator[AgentRunResultEvent[ScriptedOutput]]:
            async def no_events() -> AsyncIterator[
                AgentRunResultEvent[ScriptedOutput]
            ]:
                return
                yield  # makes this an async generator

            return no_events()

    silent_send, _silent_receive = anyio.create_memory_object_stream[
        ReactStreamEvent[ScriptedOutput]
    ](100)
    silent_deps = _deps(agent=cast("Agent[None, ScriptedOutput]", SilentAgent()))
    silent_deps.event_send_stream = silent_send

    with pytest.raises(RuntimeError, match="did not yield AgentRunResultEvent"):
        await RunAgent[ScriptedOutput, None](user_prompt="hi").run(_ctx(silent_deps))


async def test_iter_react_graph_events_reraises_graph_error_after_drain() -> None:
    """A graph error is captured and re-raised in the consumer's own frame —
    the consumer is never cancelled mid-event by the failing background task."""

    async def boom(
        messages: list[ModelMessage], info: AgentInfo
    ) -> AsyncIterator[str]:
        raise RuntimeError("model boom")
        yield ""  # pragma: no cover - marks this an async generator

    agent: Agent[None, ScriptedOutput] = Agent(
        FunctionModel(stream_function=boom, model_name="scripted"),
        deps_type=type(None),
        output_type=[str, DeferredToolRequests],
        toolsets=[inkling_toolset],
        system_prompt=SYSTEM_PROMPT,
    )
    deps = _deps(agent=agent)
    received: list[ReactStreamEvent[ScriptedOutput]] = []

    with pytest.raises(RuntimeError, match="model boom"):
        async for event in iter_react_graph_events(
            StartTurn(user_prompt="hi"),
            state=ReactState(conversation_key=_key(), agent_tentacle_id="inkling"),
            deps=deps,
        ):
            received.append(event)

    assert not any(isinstance(event, AgentRunResultEvent) for event in received)
