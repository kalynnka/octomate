from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncGenerator
from contextlib import aclosing, asynccontextmanager

import anyio
import pytest
from anyio.streams.memory import MemoryObjectReceiveStream
from pydantic import TypeAdapter
from pydantic_ai.messages import PartDeltaEvent, TextPartDelta
from uuid_utils.compat import uuid7

from octomate.fibers import (
    FIBER_BUFFER,
    MAX_FRAME_BYTES,
    FiberError,
    MemoryFiber,
    StdioFiber,
)
from octomate.nerve import (
    ChannelTopic,
    Impulse,
    Nerve,
    NerveEvent,
    Publication,
    SandboxTopic,
    ThreadTopic,
    Topic,
    UserTopic,
    publication_adapter,
)
from octomate.schemas.conversation import ChannelAddress
from octomate.schemas.sandbox import CancelInvocation
from octomate.schemas.thread import ThreadKey
from tests.support.nerve import held

HERE = ChannelAddress("web", "thread", "u", "u", channel_thread_id="asking")
THERE = ChannelAddress("web", "thread", "u", "u", channel_thread_id="destination")
EVENT = PartDeltaEvent(index=0, delta=TextPartDelta(content_delta="hello"))


@asynccontextmanager
async def connected(
    core: Nerve,
    topic: Topic,
) -> AsyncGenerator[tuple[StdioFiber[Impulse[NerveEvent]], StdioFiber[Publication]]]:
    to_guest, from_core = anyio.create_memory_object_stream[bytes](10)
    to_core, from_guest = anyio.create_memory_object_stream[bytes](10)
    core_fiber = StdioFiber(from_guest, to_guest, Publication.adapter(topic))
    guest_fiber = StdioFiber(from_core, to_core, publication_adapter)
    async with (
        aclosing(core_fiber),
        aclosing(guest_fiber),
        core.subscribe(topic, fiber=core_fiber),
        core.publish(topic, fiber=core_fiber),
    ):
        core_fiber.start()
        guest_fiber.start()
        yield core_fiber, guest_fiber


async def test_memory_fiber_keeps_local_stream_ends() -> None:
    fiber = MemoryFiber[int](buffer=1)
    try:
        fiber.send.send_nowait(7)
        with pytest.raises(anyio.WouldBlock):
            fiber.send.send_nowait(8)
        assert await fiber.receive.receive() == 7
    finally:
        fiber.close()


async def test_stdio_fiber_transports_items_without_a_nerve() -> None:
    sending, incoming = anyio.create_memory_object_stream[bytes](2)
    outgoing, receiving = anyio.create_memory_object_stream[bytes](2)
    fiber = StdioFiber(incoming, outgoing, TypeAdapter(int))
    fiber.start()
    try:
        fiber.send.send_nowait(41)
        assert await receiving.receive() == b"41\n"
        await sending.send(b"4")
        await sending.send(b"2\n")
        assert await fiber.receive.receive() == 42
        assert fiber.receive.statistics().current_buffer_used == 0
    finally:
        await fiber.aclose()
        sending.close()
        receiving.close()


async def test_signals_and_events_cross_fibers_without_echo() -> None:
    core = Nerve()
    topic = SandboxTopic(uuid7())
    invocation = uuid7()
    sent = Impulse(CancelInvocation(invocation_id=invocation), invocation_id=invocation)
    reply = Impulse(EVENT, THERE, private=True, invocation_id=invocation)
    async with connected(core, topic) as (_, guest):
        async with (
            core.subscribe(topic) as core_events,
            core.subscribe(ChannelTopic("web")) as unrelated,
        ):
            core.deliver(topic, sent)
            assert await core_events.receive() == sent
            assert await guest.receive.receive() == Publication(
                topic=topic, impulse=sent
            )
            await guest.send.send(Publication(topic=topic, impulse=reply))
            assert await core_events.receive() == reply
            assert held(core_events) == []
            assert held(guest.receive) == []
            assert held(unrelated) == []


async def test_one_core_mixes_local_subscribers_and_two_user_stdio_fibers() -> None:
    core = Nerve()
    alice, bob = SandboxTopic(uuid7()), SandboxTopic(uuid7())
    ui = UserTopic(alice.user_id)
    async with (
        connected(core, alice) as (alice_fiber, alice_client),
        connected(core, bob) as (_, bob_client),
    ):
        async with (
            core.subscribe(alice) as local_alice,
            core.subscribe(ui) as local_ui,
            core.subscribe(bob) as local_bob,
        ):
            assert isinstance(local_alice, MemoryObjectReceiveStream)
            assert isinstance(local_ui, MemoryObjectReceiveStream)
            assert alice_fiber in core.subscribers[alice]
            assert any(
                isinstance(fiber, MemoryFiber) for fiber in core.subscribers[alice]
            )
            core.deliver(alice, Impulse(EVENT))
            assert await local_alice.receive() == Impulse(EVENT)
            assert await alice_client.receive.receive() == Publication(
                topic=alice, impulse=Impulse(EVENT)
            )
            assert held(bob_client.receive) == []
            assert held(local_ui) == []
            await alice_fiber.aclose()
            core.deliver(ui, Impulse(EVENT))
            core.deliver(alice, Impulse(EVENT))
            core.deliver(bob, Impulse(EVENT))
            assert await local_ui.receive() == Impulse(EVENT)
            assert await local_alice.receive() == Impulse(EVENT)
            assert await local_bob.receive() == Impulse(EVENT)
            assert await bob_client.receive.receive() == Publication(
                topic=bob, impulse=Impulse(EVENT)
            )
            assert held(alice_client.receive) == []
    assert not core.subscribers


@pytest.mark.parametrize("private", [False, True])
async def test_publish_keeps_channel_thread_origin_and_private_routing(
    private: bool,
) -> None:
    core = Nerve()
    channel = ChannelTopic("web")
    origin = ThreadTopic(ThreadKey.from_address(HERE))
    destination = ThreadTopic(ThreadKey.from_address(THERE))
    invocation = uuid7()
    async with (
        connected(core, channel) as (_, channel_client),
        connected(core, origin) as (_, origin_client),
        connected(core, destination) as (_, destination_client),
    ):
        async with (
            core.subscribe(channel) as core_channel,
            core.subscribe(origin) as core_origin,
            core.subscribe(destination) as core_destination,
        ):
            async with core.publish(HERE) as impulses:
                await impulses.send(
                    Impulse(EVENT, THERE, private=private, invocation_id=invocation)
                )
            expected = Impulse(
                EVENT,
                THERE,
                private=private,
                origin=origin.key,
                invocation_id=invocation,
            )
            assert await core_channel.receive() == expected
            assert await core_destination.receive() == expected
            if private:
                assert held(core_origin) == []
            else:
                assert await core_origin.receive() == expected
            assert held(core_channel) == []
            assert await channel_client.receive.receive() == Publication(
                topic=channel, impulse=expected
            )
            assert await destination_client.receive.receive() == Publication(
                topic=destination, impulse=expected
            )
            if private:
                assert held(origin_client.receive) == []
            else:
                assert await origin_client.receive.receive() == Publication(
                    topic=origin, impulse=expected
                )
            assert held(channel_client.receive) == []
            assert held(destination_client.receive) == []


async def test_remote_publications_keep_local_backlog_limit_and_no_replay() -> None:
    core = Nerve()
    topic = SandboxTopic(uuid7())
    async with connected(core, topic) as (_, guest):
        async with (
            core.subscribe(topic, backlog=1) as behind,
            core.subscribe(topic) as current,
        ):
            for _ in range(3):
                await guest.send.send(Publication(topic=topic, impulse=Impulse(EVENT)))
            assert [await current.receive() for _ in range(3)] == [Impulse(EVENT)] * 3
            assert [item async for item in behind] == [Impulse(EVENT)]
        async with core.subscribe(topic) as reopened:
            assert held(reopened) == []


@pytest.mark.parametrize(
    "fault", ["json", "version", "kind", "extra", "foreign", "oversized", "partial"]
)
async def test_bad_peer_disconnects_only_its_fiber(fault: str) -> None:
    nerve = Nerve()
    topic = SandboxTopic(uuid7())
    sending, incoming = anyio.create_memory_object_stream[bytes](2)
    outgoing, receiving = anyio.create_memory_object_stream[bytes](2)
    fiber = StdioFiber(incoming, outgoing, Publication.adapter(topic))
    frame = Publication(
        topic=topic, impulse=Impulse(CancelInvocation(invocation_id=uuid7()))
    )
    payload = json.loads(frame.model_dump_json())
    if fault == "version":
        payload["version"] = 999
    if fault == "kind":
        payload["impulse"]["event"]["kind"] = "unknown"
    if fault == "extra":
        payload["impulse"]["event"]["secret"] = "must-not-be-echoed"
    if fault == "foreign":
        payload["topic"]["user_id"] = str(uuid7())
    wire = json.dumps(payload).encode() + b"\n"
    if fault == "json":
        wire = b"must-not-be-echoed\n"
    if fault == "oversized":
        wire = b"x" * (MAX_FRAME_BYTES + 1) + b"\n"
    if fault == "partial":
        wire = wire[:-1]
    async with aclosing(fiber), nerve.publish(topic, fiber=fiber):
        async with (
            nerve.subscribe(topic) as impulses,
            nerve.subscribe(topic, fiber=fiber),
        ):
            fiber.start()
            try:
                await sending.send(wire)
                sending.close()
                with pytest.raises(FiberError) as raised:
                    await fiber.wait_closed()
                assert "must-not-be-echoed" not in str(raised.value)
                assert held(impulses) == []
                assert fiber.closed
                nerve.deliver(topic, Impulse(EVENT))
                assert await impulses.receive() == Impulse(EVENT)
            finally:
                receiving.close()
    assert not nerve.subscribers


async def test_signals_cannot_leak_to_ui_topics() -> None:
    nerve = Nerve()
    topic = UserTopic(uuid7())
    async with nerve.subscribe(topic) as ui:
        with pytest.raises(ValueError, match="internal sandbox topic"):
            nerve.deliver(topic, Impulse(CancelInvocation(invocation_id=uuid7())))
        assert held(ui) == []


async def test_supplied_publisher_rejects_signals_on_a_ui_topic() -> None:
    nerve = Nerve()
    topic = UserTopic(uuid7())
    async with connected(nerve, topic) as (fiber, guest):
        async with nerve.subscribe(topic) as ui:
            await guest.send.send(
                Publication(
                    topic=topic,
                    impulse=Impulse(CancelInvocation(invocation_id=uuid7())),
                )
            )
            with pytest.raises(FiberError, match="topic permissions"):
                await fiber.wait_closed()
            assert held(ui) == []
            nerve.deliver(topic, Impulse(EVENT))
            assert await ui.receive() == Impulse(EVENT)


async def test_eof_leaves_local_subscriptions_open() -> None:
    core = Nerve()
    topic = SandboxTopic(uuid7())
    async with connected(core, topic) as (core_fiber, guest):
        async with core.subscribe(topic) as local:
            await guest.aclose()
            await core_fiber.wait_closed()
            core.deliver(topic, Impulse(EVENT))
            assert await local.receive() == Impulse(EVENT)


async def test_cancelling_a_waiter_does_not_cancel_the_fiber() -> None:
    core = Nerve()
    topic = SandboxTopic(uuid7())
    async with connected(core, topic) as (core_fiber, guest):
        waiting = asyncio.create_task(core_fiber.wait_closed())
        await asyncio.sleep(0)
        waiting.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiting
        assert not core_fiber.closed
        async with core.subscribe(topic) as events:
            await guest.send.send(Publication(topic=topic, impulse=Impulse(EVENT)))
            assert await events.receive() == Impulse(EVENT)


async def test_overflow_disconnects_peer_without_interrupting_local_publisher() -> None:
    nerve = Nerve()
    channel = ChannelTopic(HERE.channel_tentacle_id)
    sending, incoming = anyio.create_memory_object_stream[bytes](0)
    outgoing, receiving = anyio.create_memory_object_stream[bytes](0)
    fiber = StdioFiber(incoming, outgoing, Publication.adapter(channel))
    async with aclosing(fiber), nerve.publish(channel, fiber=fiber):
        async with (
            nerve.subscribe(channel, fiber=fiber),
            nerve.subscribe(channel) as local,
        ):
            fiber.start()
            try:
                async with asyncio.timeout(2), nerve.publish(HERE) as impulses:
                    for _ in range(FIBER_BUFFER * 3):
                        await impulses.send(Impulse(EVENT, HERE))
                assert len(held(local)) == FIBER_BUFFER * 3
                assert fiber.closed
                assert fiber.failure is not None
            finally:
                sending.close()
                receiving.close()
    assert not nerve.subscribers


async def test_immediate_close_releases_stdio_before_reader_task_starts() -> None:
    sending, incoming = anyio.create_memory_object_stream[bytes](0)
    outgoing, receiving = anyio.create_memory_object_stream[bytes](0)
    nerve = Nerve()
    topic = SandboxTopic(uuid7())
    fiber = StdioFiber(incoming, outgoing, Publication.adapter(topic))
    async with nerve.publish(topic, fiber=fiber):
        async with nerve.subscribe(topic, fiber=fiber):
            connection = fiber.start()
            await fiber.aclose()
            assert connection.cancelled()
    assert not nerve.subscribers
    with pytest.raises(anyio.EndOfStream):
        await receiving.receive()
    with pytest.raises(anyio.BrokenResourceError):
        await sending.send(b"unused")
    sending.close()
    receiving.close()
