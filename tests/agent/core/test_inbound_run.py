"""The receiving tentacle owns execution while one kick spans agent handoffs."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator
from dataclasses import dataclass, field
from typing import Literal

import pytest
from pydantic_ai.tools import DeferredToolResults
from uuid_utils.compat import uuid7

from octomate import Octomate
from octomate.config import ChannelConfig, ChannelStreamConfig
from octomate.database import async_session
from octomate.events import (
    ReflexAsleepEvent,
    ReflexAwakeEvent,
    RunStartedEvent,
)
from octomate.fibers import FiberError
from octomate.managers.gateway import OctomateSession
from octomate.nerve import AgentTopic, ThreadTopic, Topic
from octomate.schemas.conversation import ChannelAddress
from octomate.schemas.events import MessageEvent
from octomate.schemas.segments import TextSegment
from octomate.schemas.thread import Kick, ThreadKey
from octomate.schemas.triage import SummonDecision
from octomate.schemas.user import UserProfile
from octomate.signals import (
    AsleepSignal,
    AwakeSignal,
    Signal,
    SummonSignal,
    UserMessageSignal,
    signal_adapter,
)
from octomate.tentacles.channel import ChannelOutput
from octomate.tentacles.deferred import DeferredSuspender
from octomate.tentacles.stream import ReactStreamEvent
from tests.support.agents import FakeAgent
from tests.support.channels import FakeChannelTentacle
from tests.support.nerve import kick_and_wait, listening, publish_signal

ADDRESS = ChannelAddress("im", "dm", "alice", "alice")


@dataclass
class WaitingAgent(FakeAgent):
    entered: asyncio.Event = field(default_factory=asyncio.Event)
    release: asyncio.Event = field(default_factory=asyncio.Event)
    closed: asyncio.Event = field(default_factory=asyncio.Event)
    fail: bool = False
    fail_on_resume: bool = False
    wait_on_resume: bool = False
    resumed: asyncio.Event = field(default_factory=asyncio.Event)

    async def reception(
        self,
        conversation_address: ChannelAddress,
        *,
        deferred_tool_results: DeferredToolResults | None,
        deferred_suspender: DeferredSuspender | None,
        session: OctomateSession | None,
    ) -> AsyncGenerator[ReactStreamEvent[ChannelOutput]]:
        try:
            self.entered.set()
            if self.wait_on_resume and deferred_tool_results is not None:
                self.release.clear()
                self.resumed.set()
            await self.release.wait()
            if self.fail or (self.fail_on_resume and deferred_tool_results is not None):
                raise RuntimeError("receiver failed")
            async for event in super().reception(
                conversation_address,
                deferred_tool_results=deferred_tool_results,
                deferred_suspender=deferred_suspender,
                session=session,
            ):
                yield event
        finally:
            self.closed.set()


@pytest.fixture
async def host(in_memory_engine) -> Octomate:
    app = Octomate()
    app.connect(
        FakeAgent(
            id="first",
            reception_summon=SummonDecision(
                agent_id="second",
                model="test",
                reason="Handoff",
                hint="Continue",
                brief="Finish the work",
            ),
        )
    )
    app.connect(WaitingAgent(id="second"))
    app.connect(
        FakeChannelTentacle(
            config=ChannelConfig(
                type="fake",
                stream=ChannelStreamConfig(enabled=False),
                agents=["first", "second"],
            )
        )
    )
    return app


@pytest.fixture
def signal() -> UserMessageSignal:
    return UserMessageSignal(
        [
            MessageEvent(
                tentacle_id="im",
                chat_type="dm",
                chat_id="alice",
                user_id="alice",
                sender=UserProfile(channel_tentacle_id="im", channel_user_id="alice"),
                segments=[TextSegment(data={"text": "Start"})],
            )
        ]
    )


@pytest.mark.parametrize("ending", ["completed", "failed", "cancelled"])
async def test_receiver_owns_execution_and_handoff(
    host: Octomate,
    signal: UserMessageSignal,
    monkeypatch: pytest.MonkeyPatch,
    ending: Literal["completed", "failed", "cancelled"],
) -> None:
    receiver = host.agents["second"]
    assert isinstance(receiver, WaitingAgent)
    receiver.fail = ending == "failed"
    recipients: list[str] = []
    dispatch = host.nerves.signals.dispatch

    def transmit(topic: Topic, sent: Signal) -> None:
        assert isinstance(topic, AgentTopic)
        if isinstance(sent, AwakeSignal):
            recipients.append(topic.agent_id)
        dispatch(topic, signal_adapter.validate_json(signal_adapter.dump_json(sent)))

    monkeypatch.setattr(host.nerves.signals, "dispatch", transmit)
    async with (
        listening(host),
        host.nerves.events.subscribe(
            ThreadTopic(ThreadKey.model_validate(ADDRESS))
        ) as events,
        asyncio.timeout(5),
    ):
        assert await publish_signal(host, signal) is None
        assert not hasattr(host, "awakes")
        assert not hasattr(host, "kick")
        await receiver.entered.wait()
        assert receiver.reflexes
        assert not host.agents["first"].reflexes
        if ending == "cancelled":
            await publish_signal(
                host, AsleepSignal(thread=ThreadKey.model_validate(ADDRESS))
            )
        else:
            receiver.release.set()
        observed = []
        async for impulse in events:
            observed.append(impulse.event)
            if isinstance(impulse.event, ReflexAsleepEvent):
                break
        assert not host.awake_since
    assert recipients == ["first", "second"]
    assert receiver.closed.is_set()
    assert not host.nerves.signals.subscribers
    assert not host.nerves.events.retained
    assert sum(isinstance(event, ReflexAwakeEvent) for event in observed) == 1
    assert observed[-1] == ReflexAsleepEvent(outcome=ending)


async def test_handoff_can_return_to_first_receiver(
    host: Octomate, signal: UserMessageSignal, monkeypatch: pytest.MonkeyPatch
) -> None:
    first, second = host.agents["first"], host.agents["second"]
    assert isinstance(first, FakeAgent)
    assert isinstance(second, WaitingAgent)
    second.release.set()
    second.reception_summon = SummonDecision(
        agent_id="first", model="test", reason="Return", hint="Finish", brief="Back"
    )
    dispatch = host.nerves.signals.dispatch

    def transmit(topic: Topic, sent: Signal) -> None:
        if (
            isinstance(topic, AgentTopic)
            and isinstance(sent, SummonSignal)
            and topic.agent_id == "first"
        ):
            first.reception_summon = None
        dispatch(topic, signal_adapter.validate_json(signal_adapter.dump_json(sent)))

    monkeypatch.setattr(host.nerves.signals, "dispatch", transmit)
    await kick_and_wait(host, signal)
    assert len(first.turns) == 2
    assert not host.awake_since


@pytest.mark.parametrize("ending", ["completed", "failed", "cancelled"])
async def test_final_thread_after_handoff_and_teleport(
    host: Octomate,
    signal: UserMessageSignal,
    ending: Literal["completed", "failed", "cancelled"],
) -> None:
    receiver = host.agents["second"]
    assert isinstance(receiver, WaitingAgent)
    receiver.release.set()
    receiver.reception_teleport = "Continue in a thread"
    receiver.fail_on_resume = ending == "failed"
    receiver.wait_on_resume = ending == "cancelled"
    async with (
        listening(host),
        host.nerves.events.subscribe(
            ThreadTopic(ThreadKey.model_validate(ADDRESS))
        ) as events,
        asyncio.timeout(5),
    ):
        await publish_signal(host, signal)
        if ending == "cancelled":
            await receiver.resumed.wait()
            await publish_signal(
                host, AsleepSignal(thread=ThreadKey.model_validate(ADDRESS))
            )
        observed = []
        async for impulse in events:
            observed.append(impulse)
            if isinstance(impulse.event, ReflexAsleepEvent):
                break
    started = [
        impulse for impulse in observed if isinstance(impulse.event, RunStartedEvent)
    ]
    assert len(started) == 3
    final_address = started[-1].address
    assert final_address is not None
    assert final_address != ADDRESS
    assert observed[-1].address == final_address
    assert observed[-1].event == ReflexAsleepEvent(outcome=ending)
    final_thread = await host.threads.get(
        ThreadKey.model_validate(final_address), with_messages=False
    )
    assert final_thread is not None
    async with async_session() as session:
        kicks = await session.list(Kick)
    assert len(kicks) == 1
    assert kicks[0].thread_id == final_thread.id
    assert kicks[0].outcome == ending


async def test_delivery_without_receiver_refuses_and_releases_status(
    host: Octomate, signal: UserMessageSignal
) -> None:
    with pytest.raises(FiberError, match="exactly one ready receiver"):
        await publish_signal(host, signal)
    assert not host.awake_since


async def test_immediate_interrupt_settles_on_the_event_stream(
    host: Octomate, signal: UserMessageSignal
) -> None:
    async with (
        listening(host),
        host.nerves.events.subscribe(
            ThreadTopic(ThreadKey.model_validate(ADDRESS))
        ) as events,
        asyncio.timeout(5),
    ):
        await publish_signal(host, signal)
        await publish_signal(
            host, AsleepSignal(thread=ThreadKey.model_validate(ADDRESS))
        )
        async for impulse in events:
            if isinstance(impulse.event, ReflexAsleepEvent):
                assert impulse.event.outcome == "cancelled"
                break
    assert not host.awake_since


async def test_receiver_shutdown_drains_accepted_signals_and_cancels_execution(
    host: Octomate, signal: UserMessageSignal
) -> None:
    async with listening(host):
        await publish_signal(host, signal)
    assert not host.awake_since
    assert all(not agent.reflexes for agent in host.agents.values())
    assert not host.nerves.signals.subscribers


async def test_agent_resources_close_after_receiver_and_reflexes(
    host: Octomate, signal: UserMessageSignal
) -> None:
    agent = host.agents["second"]
    assert isinstance(agent, WaitingAgent)
    closed: list[bool] = []

    async def close_resource() -> None:
        assert agent.closed.is_set()
        assert not agent.reflexes
        assert not host.nerves.signals.subscribers
        closed.append(True)

    agent.resources.push_async_callback(close_resource)
    async with agent, asyncio.timeout(5):
        host.nerves.signals.dispatch(
            AgentTopic(agent.id), UserMessageSignal(signal.messages, agent_id=agent.id)
        )
        await agent.entered.wait()

    assert closed == [True]


async def test_agent_resources_unwind_after_cleanup_failure(host: Octomate) -> None:
    agent = host.agents["second"]
    closed: list[bool] = []

    def fail_cleanup() -> None:
        raise RuntimeError("resource cleanup failed")

    agent.resources.callback(closed.append, True)
    agent.resources.callback(fail_cleanup)
    with pytest.raises(RuntimeError, match="resource cleanup failed"):
        async with agent:
            pass

    assert closed == [True]
    assert not agent.routes
    assert not host.nerves.signals.subscribers


async def test_sensing_failure_closes_delivery_and_releases_resources(
    host: Octomate,
) -> None:
    agent = host.agents["second"]
    closed: list[bool] = []
    agent.resources.callback(closed.append, True)

    await agent.__aenter__()
    try:
        host.nerves.signals.dispatch(
            AgentTopic(agent.id), AsleepSignal(invocation_id=uuid7())
        )
        assert agent.sensing_task is not None
        done, _ = await asyncio.wait({agent.sensing_task}, timeout=5)
        assert done
        with pytest.raises(FiberError, match="exactly one ready receiver"):
            host.nerves.signals.dispatch(
                AgentTopic(agent.id), AsleepSignal(invocation_id=uuid7())
            )
    finally:
        with pytest.raises(NotImplementedError, match="Invocation-addressed"):
            await agent.__aexit__()

    assert closed == [True]
    assert agent.signals is None
    assert agent.sensing_task is None
    assert not host.nerves.signals.subscribers
