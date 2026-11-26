"""Inbound dispatch borrows core's resources and preserves the provider stream."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator
from pathlib import Path
from unittest.mock import Mock

import pytest
from pydantic_ai import AgentRunResult, AgentRunResultEvent
from uuid_utils.compat import uuid7

from octomate import Octomate
from octomate.fibers import FiberError
from octomate.managers.habitat import Invocation
from octomate.nerve import InvocationTopic
from octomate.reflex.suspender import ReflexSuspender
from octomate.schemas.conversation import ChannelAddress, Conversation
from octomate.signals import Priming, RunAgentSignal, signal_adapter
from octomate.tentacles.stream import ReactEventStream, ReactStreamEvent
from tests.support.agents import FakeAgent
from tests.support.invocations import a_native_invocation

ADDRESS = ChannelAddress("web", "dm", "user", "user")


@pytest.fixture
def host() -> Octomate:
    return Octomate()


@pytest.fixture
def agent(host: Octomate) -> FakeAgent:
    return FakeAgent(id="agent", octomate=host)


@pytest.fixture
def invocation(agent: FakeAgent, tmp_path: Path) -> Invocation:
    conversation = Conversation(thread_id=uuid7(), agent_tentacle_id=agent.id)
    return a_native_invocation(conversation, tmp_path)


@pytest.fixture
def suspender(host: Octomate, agent: FakeAgent) -> ReflexSuspender:
    return ReflexSuspender(
        action_manager=host.deferred_actions,
        conversation_manager=host.conversations,
        agent_tentacle_id=agent.id,
        run_name="react",
        source_address=ADDRESS,
        target_address=ADDRESS,
        target_mode="main",
        decision=None,
    )


@pytest.fixture
def signal(invocation: Invocation) -> RunAgentSignal:
    return RunAgentSignal(
        priming=Priming(
            invocation_id=invocation.id,
            session_id=None,
            resume=False,
            model="test",
            effort="high",
            cwd=Path(invocation.cwd),
        ),
        conversation_id=invocation.conversation.id,
        user_id=None,
        conversation_address=ADDRESS,
        source_thread_address=ADDRESS,
        source_thread_message_ids=[uuid7()],
        run_name="react",
        user_prompt="hello",
    )


@pytest.mark.parametrize("continue_tool", [False, True])
async def test_signal_starts_the_existing_provider_stream(
    host: Octomate,
    agent: FakeAgent,
    invocation: Invocation,
    suspender: ReflexSuspender,
    signal: RunAgentSignal,
    continue_tool: bool,
) -> None:
    if continue_tool:
        signal = signal.model_copy(
            update={"user_prompt": None, "tool_results": {"move": "arrived"}}
        )
    sent = signal_adapter.validate_json(signal_adapter.dump_json(signal))
    async with agent.reflex(
        invocation=invocation, deferred_suspender=suspender, capabilities=[]
    ) as stream:
        assert not agent.turns
        host.nerves.signals.dispatch(InvocationTopic(invocation.id), sent)
        assert not agent.turns
        first = await anext(stream)
        with pytest.raises(FiberError, match="exactly one"):
            host.nerves.signals.dispatch(InvocationTopic(invocation.id), sent)
        events = [first, *[event async for event in stream]]
    assert len(events) == 1
    assert isinstance(events[0], AgentRunResultEvent)
    assert events[0].result.output == "handled"
    [turn] = agent.turns
    assert turn.invocation is invocation
    assert turn.prompt == signal.user_prompt
    assert turn.model == "fake-model"
    assert turn.effort == "high"
    assert turn.source_thread_message_ids == signal.source_thread_message_ids
    if continue_tool:
        assert turn.deferred_results is not None
        assert turn.deferred_results.calls == {"move": "arrived"}
    assert not host.nerves.signals.subscribers


@pytest.mark.parametrize("field", ["invocation_id", "conversation_id", "user_id"])
async def test_signal_cannot_use_another_invocations_resources(
    host: Octomate,
    agent: FakeAgent,
    invocation: Invocation,
    suspender: ReflexSuspender,
    signal: RunAgentSignal,
    field: str,
) -> None:
    if field == "invocation_id":
        signal = signal.model_copy(
            update={"priming": signal.priming.model_copy(update={field: uuid7()})}
        )
    else:
        signal = signal.model_copy(update={field: uuid7()})
    async with agent.reflex(
        invocation=invocation, deferred_suspender=suspender, capabilities=[]
    ) as stream:
        host.nerves.signals.dispatch(InvocationTopic(invocation.id), signal)
        with pytest.raises(ValueError, match="does not belong"):
            await anext(stream)
    assert not agent.turns
    assert not host.nerves.signals.subscribers


@pytest.mark.parametrize("cancel", [False, True])
async def test_failure_and_cancellation_close_the_original_stream(
    host: Octomate,
    agent: FakeAgent,
    invocation: Invocation,
    suspender: ReflexSuspender,
    signal: RunAgentSignal,
    monkeypatch: pytest.MonkeyPatch,
    cancel: bool,
) -> None:
    entered = asyncio.Event()
    release = asyncio.Event()
    closed = asyncio.Event()

    async def events() -> AsyncGenerator[ReactStreamEvent[str], None]:
        try:
            entered.set()
            yield AgentRunResultEvent(AgentRunResult("started"))
            await release.wait()
            raise RuntimeError("provider failed")
        finally:
            closed.set()

    provider = Mock(return_value=ReactEventStream(events()))
    monkeypatch.setattr(agent, "run_stream_events", provider)

    async def run() -> None:
        async with agent.reflex(
            invocation=invocation, deferred_suspender=suspender, capabilities=[]
        ) as stream:
            host.nerves.signals.dispatch(InvocationTopic(invocation.id), signal)
            async for _event in stream:
                pass

    task = asyncio.create_task(run())
    await entered.wait()
    if cancel:
        task.cancel()
    else:
        release.set()
    with pytest.raises(asyncio.CancelledError if cancel else RuntimeError):
        await task
    assert closed.is_set()
    assert not host.nerves.signals.subscribers
    assert provider.call_args.kwargs["invocation"] is invocation
    assert provider.call_args.kwargs["deferred_suspender"] is suspender
