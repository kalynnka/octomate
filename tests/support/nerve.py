"""The nerve in a test: a thread's live events as the console reads them, and a
channel's siphon drawing what a kick published, to the end."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncGenerator
from contextlib import AsyncExitStack, asynccontextmanager
from typing import Literal

import anyio
from anyio.abc import ObjectReceiveStream
from anyio.streams.memory import MemoryObjectReceiveStream, MemoryObjectSendStream
from pydantic_ai._utils import now_utc

from octomate import Octomate
from octomate.events import (
    ReflexAsleepEvent,
    ReflexAwakeEvent,
    RunErrorEvent,
    wire_event_adapter,
)
from octomate.fibers import Fiber, MemoryFiber, ReplayFiber
from octomate.nerve import AgentTopic, ChannelTopic, Impulse, ThreadTopic
from octomate.reflex.graph import ReflexGraphResult
from octomate.schemas.conversation import ChannelAddress
from octomate.schemas.thread import ThreadKey
from octomate.signals import (
    AsleepSignal,
    AwakeSignal,
    CommandSignal,
    DeferredActionBatchResponse,
    DrivenGatewaySignal,
    NativeGatewaySignal,
    Signal,
    SummonSignal,
    UserMessageSignal,
)
from octomate.tentacles.agent import AgentTentacle
from octomate.tentacles.channel import ChannelTentacle
from octomate.types.json import JsonObject


async def settled(live: ObjectReceiveStream[Impulse]) -> list[JsonObject]:
    """The kick's events as the console reads them off its socket, up to the
    reflex falling asleep."""
    events: list[JsonObject] = []
    async for impulse in live:
        if isinstance(impulse.event, ReflexAsleepEvent):
            return events
        events.append(
            json.loads(wire_event_adapter.dump_json(impulse.event, warnings=False))
        )
    raise AssertionError("the turn never settled")


def held[ItemT](live: ObjectReceiveStream[ItemT]) -> list[ItemT]:
    """Everything the nerve has handed `live` that it has yet to read, taken now."""
    assert isinstance(live, Fiber | MemoryObjectReceiveStream)
    receiver = live.receiver if isinstance(live, Fiber) else live
    replayed = len(live.kept) if isinstance(live, ReplayFiber) else 0
    return [
        live.receive_nowait()
        for _ in range(replayed + receiver.statistics().current_buffer_used)
    ]


@asynccontextmanager
async def listening(octomate: Octomate) -> AsyncGenerator[None]:
    """Start only signal receivers for tests that do not boot provider clients."""
    receivers: list[tuple[MemoryFiber[Signal], asyncio.Task[None]]] = []
    agents = [
        agent
        for agent in octomate.agents.values()
        if AgentTopic(agent.id) not in octomate.nerves.signals.subscribers
    ]
    async with AsyncExitStack() as stack:
        for agent in agents:
            signals = MemoryFiber[Signal]()
            stack.enter_context(
                octomate.nerves.signals.accept(AgentTopic(agent.id), signals)
            )
            receiver = asyncio.create_task(agent.sensing(signals))
            receiver.add_done_callback(lambda task, signals=signals: signals.close())
            receivers.append((signals, receiver))
        try:
            yield
        finally:
            for signals, _ in receivers:
                signals.close_send()
            try:
                await asyncio.gather(*(receiver for _, receiver in receivers))
            finally:
                tasks = [
                    task
                    for agent in agents
                    for running in agent.reflexes.values()
                    for task in running
                ]
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)


@asynccontextmanager
async def drawing(*channels: ChannelTentacle) -> AsyncGenerator[None]:
    """The channels' siphons started, as entering a channel starts its own. Leaving
    ends each one's stream on its host's nerve, and returns once every siphon has
    drawn everything it heard — what a kick published is on the page by then."""
    async with AsyncExitStack() as stack:
        for channel in channels:
            await stack.enter_async_context(listening(channel.octomate))
        for channel in channels:
            await channel.siphon.__aenter__()
        try:
            yield
        finally:
            for channel in channels:
                for fiber in channel.octomate.nerves.events.subscribers.get(
                    ChannelTopic(channel.id), set()
                ):
                    fiber.close_send()
                if channel.siphon.reading is not None:
                    await channel.siphon.reading
                await channel.siphon.__aexit__(None, None, None)


async def drained(*channels: ChannelTentacle) -> None:
    """Returns once each started channel's siphon has drawn everything handed to
    it so far. The caller owns the deadline, including any test-wide timeout."""
    # A started siphon tells no one it is done; what it still holds shows it.
    while any(  # noqa: ASYNC110
        channel.siphon.lanes
        or any(
            fiber.receiver.statistics().current_buffer_used
            for fiber in channel.octomate.nerves.events.subscribers.get(
                ChannelTopic(channel.id), set()
            )
        )
        for channel in channels
    ):
        await asyncio.sleep(0)


@asynccontextmanager
async def awake_on(octomate: Octomate, address: ChannelAddress) -> AsyncGenerator[None]:
    """A reflex awake on the thread at `address` for the block, doing nothing."""

    thread = ThreadKey.model_validate(address)
    octomate.awake_since[thread] = now_utc()
    try:
        yield
    finally:
        octomate.awake_since.pop(thread)
        async with octomate.nerves.events.publish(address) as events:
            await events.send(Impulse(ReflexAsleepEvent(outcome="completed"), address))


async def publish_signal(octomate: Octomate, signal: Signal) -> None:
    """Build a test recipient explicitly; production producers own this selection."""
    match signal:
        case AsleepSignal():
            for agent_id in octomate.agents:
                octomate.nerves.signals.deliver(AgentTopic(agent_id), signal)
            return
        case UserMessageSignal():
            thread = await octomate.threads.get(
                ThreadKey.model_validate(signal.address), with_messages=False
            )
            agent_id = (
                signal.agent_id
                or (thread.active_agent_tentacle_id if thread is not None else None)
                or octomate.channels[signal.address.channel_tentacle_id].agent_ids[0]
            )
        case CommandSignal():
            agent_id = signal.context.agent_id
        case DeferredActionBatchResponse():
            batch = await octomate.deferred_actions.get_batch(signal.batch_id)
            agent_id = batch.agent_tentacle_id
        case SummonSignal():
            agent_id = signal.decision.agent_id
        case NativeGatewaySignal() | DrivenGatewaySignal():
            agent_id = signal.agent_id
        case _:
            raise AssertionError("Unsupported signal")
    octomate.nerves.signals.dispatch(AgentTopic(agent_id), signal)


async def interrupt_and_wait(
    agent: AgentTentacle,
    signal: UserMessageSignal | CommandSignal,
    ready: asyncio.Event,
    *,
    target: Literal["thread", "invocation"],
) -> None:
    address = (
        signal.address
        if isinstance(signal, UserMessageSignal)
        else signal.context.address
    )
    thread = ThreadKey.model_validate(address)
    host = agent.octomate
    async with (
        listening(host),
        host.nerves.events.subscribe(ThreadTopic(thread)) as events,
        asyncio.timeout(5),
    ):
        await publish_signal(host, signal)
        await ready.wait()
        tasks = list(agent.reflexes[thread])
        interruption = (
            AsleepSignal(thread=thread)
            if target == "thread"
            else AsleepSignal(invocation_id=next(iter(agent.invocations)))
        )
        await publish_signal(host, interruption)
        await publish_signal(host, interruption)
        async for impulse in events:
            if isinstance(impulse.event, ReflexAsleepEvent):
                assert impulse.event.outcome == "cancelled"
                break
        await publish_signal(host, interruption)
        await asyncio.gather(*tasks)
        assert all(not task.cancelling() for task in tasks)
    assert not agent.invocations
    assert not agent.reflexes
    assert thread not in host.awake_since


async def kick_and_wait(
    octomate: Octomate, signal: AwakeSignal
) -> ReflexGraphResult | None:
    """Observe completion; inspect receiver tasks only for graph result assertions."""
    async with listening(octomate):
        if isinstance(signal, DeferredActionBatchResponse):
            batch = await octomate.deferred_actions.get_batch(signal.batch_id)
            if batch.response_mode != "resume":
                pending = octomate.agents[batch.agent_tentacle_id].pendings.get(
                    signal.batch_id
                )
                async with octomate.nerves.events.subscribe(
                    ThreadTopic(ThreadKey.model_validate(batch.source_address))
                ) as events:
                    await publish_signal(octomate, signal)
                    if pending is not None:
                        await pending
                        return None
                    error = (await events.receive()).event
                assert isinstance(error, RunErrorEvent)
                raise RuntimeError(error.message)
        match signal:
            case UserMessageSignal():
                origin = signal.address
            case CommandSignal():
                origin = signal.context.address
            case SummonSignal():
                origin = signal.origin or signal.destination
            case NativeGatewaySignal() | DrivenGatewaySignal():
                origin = signal.source
            case DeferredActionBatchResponse():
                origin = batch.source_address
            case _:
                raise AssertionError("Unsupported awake")
        assert origin is not None
        async with octomate.nerves.events.subscribe(
            ThreadTopic(ThreadKey.model_validate(origin))
        ) as events:
            await publish_signal(octomate, signal)
            tasks: set[asyncio.Task[ReflexGraphResult | None]] = set()
            started = False
            async for impulse in events:
                if isinstance(impulse.event, ReflexAwakeEvent):
                    started = True
                for agent in octomate.agents.values():
                    tasks.update(
                        task for running in agent.reflexes.values() for task in running
                    )
                if started and isinstance(impulse.event, ReflexAsleepEvent):
                    results = await asyncio.gather(*tasks)
                    return results[-1] if results else None
        raise AssertionError("The reflex never fell asleep")


async def finished(octomate: Octomate, thread: ThreadKey) -> None:
    """Observe a dispatched turn finishing without reaching into its execution task."""
    async with octomate.nerves.events.subscribe(
        ThreadTopic(thread), replay=0
    ) as events:
        await settled(events)


async def kicked(octomate: Octomate, signal: AwakeSignal) -> ReflexGraphResult | None:
    """`kick_and_wait` on a host whose channels were never entered, returning once
    each has drawn what the kick published."""
    async with drawing(*octomate.channels.values()):
        return await kick_and_wait(octomate, signal)


def unwired() -> MemoryObjectSendStream[Impulse]:
    """A send end for deps built ahead of the kick that opens the real one: it
    refuses anything sent on it."""
    send, receive = anyio.create_memory_object_stream[Impulse]()
    receive.close()
    return send
