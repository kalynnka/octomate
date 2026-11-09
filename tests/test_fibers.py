from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncGenerator
from contextlib import aclosing, asynccontextmanager
from typing import assert_type

import anyio
import pytest
from pydantic import TypeAdapter, ValidationError
from pydantic_ai.messages import PartDeltaEvent, TextPartDelta
from uuid_utils.compat import uuid7

from octomate.capabilities.harness.events import RunEndedEvent, RunStartedEvent
from octomate.fibers import (
    FIBER_BUFFER,
    MAX_FRAME_BYTES,
    FiberError,
    MemoryFiber,
    StdioFiber,
)
from octomate.nerve import (
    ChannelTopic,
    HabitatTopic,
    Impulse,
    Nerve,
    ThreadTopic,
    Topic,
    UserTopic,
    impulse_adapter,
)
from octomate.schemas.conversation import ChannelAddress
from octomate.schemas.habitat import HabitatFailed
from octomate.schemas.thread import ThreadKey
from tests.support.nerve import held

HERE = ChannelAddress("web", "thread", "u", "u", channel_thread_id="asking")
THERE = ChannelAddress("web", "thread", "u", "u", channel_thread_id="destination")
EVENT = PartDeltaEvent(index=0, delta=TextPartDelta(content_delta="hello"))


@asynccontextmanager
async def connected(
    core: Nerve,
    topic: Topic,
    *,
    publishing: bool = True,
) -> AsyncGenerator[tuple[StdioFiber[Impulse], StdioFiber[Impulse]]]:
    sending, receiving = anyio.create_memory_object_stream[bytes](10)
    sender = StdioFiber(impulse_adapter, output=sending)
    receiver = StdioFiber(impulse_adapter, input=receiving)
    core_fiber, guest_fiber = (receiver, sender) if publishing else (sender, receiver)
    binding = (
        core.publish(topic, fiber=core_fiber)
        if publishing
        else core.subscribe(topic, fiber=core_fiber)
    )
    async with aclosing(core_fiber), aclosing(guest_fiber), binding:
        core_fiber.start()
        guest_fiber.start()
        yield core_fiber, guest_fiber


async def test_memory_fiber_keeps_local_stream_ends() -> None:
    fiber = MemoryFiber[int](buffer=1)
    try:
        fiber.send_nowait(7)
        with pytest.raises(anyio.WouldBlock):
            fiber.send_nowait(8)
        assert await fiber.receive() == 7
    finally:
        fiber.close()


async def test_stdio_fiber_transports_items_without_a_nerve() -> None:
    sending, receiving = anyio.create_memory_object_stream[bytes](2)
    async with (
        StdioFiber(TypeAdapter(int), output=sending) as sender,
        StdioFiber(TypeAdapter(int), input=receiving) as receiver,
    ):
        sender.start()
        receiver.start()
        await sender.send(41)
        sender.send_nowait(42)
        sender.close_send()
        await sender.wait_closed()
        assert [item async for item in receiver] == [41, 42]
        await receiver.wait_closed()


async def test_stdio_adapter_preserves_its_concrete_impulse_type() -> None:
    adapter: TypeAdapter[Impulse[PartDeltaEvent]] = TypeAdapter(Impulse[PartDeltaEvent])
    with pytest.raises(ValidationError):
        adapter.validate_json(impulse_adapter.dump_json(Impulse(RunEndedEvent())))
    sending, receiving = anyio.create_memory_object_stream[bytes](2)
    async with (
        StdioFiber(adapter, output=sending) as sender,
        StdioFiber(adapter, input=receiving) as receiver,
    ):
        assert_type(sender, StdioFiber[Impulse[PartDeltaEvent]])
        assert_type(receiver, StdioFiber[Impulse[PartDeltaEvent]])
        sender.start()
        receiver.start()
        await sender.send(Impulse(EVENT))
        received = await receiver.receive()
        assert_type(received.event, PartDeltaEvent)
        assert received.event == EVENT


async def test_stdio_fiber_reassembles_fragmented_frames() -> None:
    sending, incoming = anyio.create_memory_object_stream[bytes](2)
    async with sending, StdioFiber(TypeAdapter(int), input=incoming) as fiber:
        fiber.start()
        await sending.send(b"4")
        await sending.send(b"2\n43\n")
        assert await fiber.receive() == 42
        assert await fiber.receive() == 43
        with pytest.raises(anyio.WouldBlock):
            fiber.receive_nowait()


async def test_stdio_fiber_requires_exactly_one_direction() -> None:
    sending, receiving = anyio.create_memory_object_stream[bytes](2)
    async with sending, receiving:
        with pytest.raises(ValueError, match="exactly one"):
            StdioFiber(TypeAdapter(int))
        with pytest.raises(ValueError, match="exactly one"):
            StdioFiber(TypeAdapter(int), input=receiving, output=sending)


async def test_stdio_fiber_rejects_the_opposite_local_endpoint() -> None:
    sending, receiving = anyio.create_memory_object_stream[bytes](2)
    async with (
        StdioFiber(TypeAdapter(int), output=sending) as sender,
        StdioFiber(TypeAdapter(int), input=receiving) as receiver,
    ):
        with pytest.raises(FiberError, match="no local receiving endpoint"):
            await sender.receive()
        with pytest.raises(FiberError, match="no local receiving endpoint"):
            sender.receive_nowait()
        with pytest.raises(FiberError, match="no local sending endpoint"):
            await receiver.send(1)
        with pytest.raises(FiberError, match="no local sending endpoint"):
            receiver.send_nowait(1)


async def test_events_flow_from_stdio_to_core() -> None:
    core = Nerve()
    topic = HabitatTopic(uuid7())
    sent: list[Impulse] = [
        Impulse(RunStartedEvent(address=THERE)),
        Impulse(EVENT, THERE, private=True),
        Impulse(RunEndedEvent()),
    ]
    async with connected(core, topic) as (core_fiber, guest):
        async with (
            core.subscribe(topic) as core_events,
            core.subscribe(ChannelTopic("web")) as unrelated,
        ):
            assert core_fiber not in core.subscribers[topic]
            for impulse in sent:
                await guest.send(impulse)
            assert [await core_events.receive() for _ in sent] == sent
            assert held(core_events) == []
            assert held(unrelated) == []


async def test_one_core_mixes_local_subscribers_and_two_user_stdio_fibers() -> None:
    core = Nerve()
    alice, bob = HabitatTopic(uuid7()), HabitatTopic(uuid7())
    sent = Impulse(RunStartedEvent(address=THERE))
    reply = Impulse(EVENT)
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
            assert isinstance(local_alice, MemoryFiber)
            assert isinstance(local_ui, MemoryFiber)
            assert core.subscribers[alice] == {local_alice}
            core.deliver(alice, sent)
            assert await local_alice.receive() == sent
            await alice_client.send(reply)
            assert await local_alice.receive() == reply
            assert held(local_bob) == []
            assert held(local_ui) == []
            await alice_fiber.aclose()
            core.deliver(ui, Impulse(EVENT))
            core.deliver(alice, reply)
            core.deliver(bob, sent)
            assert await local_ui.receive() == Impulse(EVENT)
            assert await local_alice.receive() == reply
            assert await local_bob.receive() == sent
            await bob_client.send(reply)
            assert await local_bob.receive() == reply
            assert held(local_alice) == []
    assert not core.subscribers


@pytest.mark.parametrize("private", [False, True])
async def test_publish_keeps_channel_thread_origin_and_private_routing(
    private: bool,
) -> None:
    core = Nerve()
    channel = ChannelTopic("web")
    origin = ThreadTopic(ThreadKey.from_address(HERE))
    destination = ThreadTopic(ThreadKey.from_address(THERE))
    async with (
        connected(core, channel, publishing=False) as (_, channel_client),
        connected(core, origin, publishing=False) as (_, origin_client),
        connected(core, destination, publishing=False) as (_, destination_client),
    ):
        async with (
            core.subscribe(channel) as core_channel,
            core.subscribe(origin) as core_origin,
            core.subscribe(destination) as core_destination,
        ):
            async with core.publish(HERE) as impulses:
                await impulses.send(Impulse(EVENT, THERE, private=private))
            expected = Impulse(
                EVENT,
                THERE,
                private=private,
                origin=origin.key,
            )
            assert await core_channel.receive() == expected
            assert await core_destination.receive() == expected
            if private:
                assert held(core_origin) == []
            else:
                assert await core_origin.receive() == expected
            assert held(core_channel) == []
            assert await channel_client.receive() == expected
            assert await destination_client.receive() == expected
            if private:
                assert held(origin_client) == []
            else:
                assert await origin_client.receive() == expected
            assert held(channel_client) == []
            assert held(destination_client) == []


async def test_remote_publications_keep_local_backlog_limit_and_no_replay() -> None:
    core = Nerve()
    topic = HabitatTopic(uuid7())
    async with connected(core, topic) as (_, guest):
        async with (
            core.subscribe(topic, backlog=1) as behind,
            core.subscribe(topic) as current,
        ):
            for _ in range(3):
                await guest.send(Impulse(EVENT))
            assert [await current.receive() for _ in range(3)] == [Impulse(EVENT)] * 3
            assert [item async for item in behind] == [Impulse(EVENT)]
        async with core.subscribe(topic) as reopened:
            assert held(reopened) == []


@pytest.mark.parametrize(
    "fault",
    [
        "json",
        "missing-event",
        "event-kind",
        "extra",
        "topic",
        "oversized",
        "partial",
    ],
)
async def test_bad_peer_disconnects_only_its_fiber(fault: str) -> None:
    nerve = Nerve()
    topic = HabitatTopic(uuid7())
    sending, incoming = anyio.create_memory_object_stream[bytes](2)
    fiber = StdioFiber(impulse_adapter, input=incoming)
    frame = Impulse(
        HabitatFailed(
            user_id=topic.user_id, habitat="local", sequence=1, message="failed"
        )
    )
    payload = json.loads(impulse_adapter.dump_json(frame))
    if fault == "missing-event":
        del payload["event"]
    if fault == "event-kind":
        payload["event"]["event_kind"] = "unknown"
    if fault == "extra":
        payload["event"]["secret"] = "must-not-be-echoed"
    if fault == "topic":
        payload["topic"] = {"kind": "user", "user_id": str(uuid7())}
    wire = json.dumps(payload).encode() + b"\n"
    if fault == "json":
        wire = b"must-not-be-echoed\n"
    if fault == "oversized":
        wire = b"x" * (MAX_FRAME_BYTES + 1) + b"\n"
    if fault == "partial":
        wire = wire[:-1]
    async with (
        sending,
        aclosing(fiber),
        nerve.publish(topic, fiber=fiber),
        nerve.subscribe(topic) as impulses,
    ):
        fiber.start()
        await sending.send(wire)
        sending.close()
        with pytest.raises(FiberError) as raised:
            await fiber.wait_closed()
        assert "must-not-be-echoed" not in str(raised.value)
        assert held(impulses) == []
        assert fiber.closed
        event = Impulse(EVENT)
        nerve.deliver(topic, event)
        assert await impulses.receive() == event
    assert not nerve.subscribers


async def test_habitat_topics_do_not_implicitly_forward_to_ui_topics() -> None:
    nerve = Nerve()
    topic = UserTopic(uuid7())
    async with nerve.subscribe(topic) as ui:
        nerve.deliver(
            HabitatTopic(topic.user_id),
            Impulse(EVENT),
        )
        assert held(ui) == []


async def test_habitat_transport_routes_only_to_the_core_bound_topic() -> None:
    nerve = Nerve()
    owner = uuid7()
    topic = HabitatTopic(owner)
    other = HabitatTopic(uuid7())
    impulse = Impulse(
        EVENT,
        HERE,
        origin=ThreadKey.from_address(HERE),
    )
    async with connected(nerve, topic) as (_, guest):
        async with (
            nerve.subscribe(topic) as events,
            nerve.subscribe(UserTopic(owner)) as ui,
            nerve.subscribe(other) as other_user,
            nerve.subscribe(ChannelTopic(HERE.channel_tentacle_id)) as channel,
            nerve.subscribe(ThreadTopic(ThreadKey.from_address(HERE))) as thread,
        ):
            await guest.send(impulse)
            assert await events.receive() == impulse
            assert held(ui) == []
            assert held(other_user) == []
            assert held(channel) == []
            assert held(thread) == []
            nerve.deliver(UserTopic(owner), Impulse(EVENT))
            assert await ui.receive() == Impulse(EVENT)


async def test_eof_leaves_local_subscriptions_open() -> None:
    core = Nerve()
    topic = HabitatTopic(uuid7())
    async with connected(core, topic) as (core_fiber, guest):
        async with core.subscribe(topic) as local:
            await guest.aclose()
            await core_fiber.wait_closed()
            core.deliver(topic, Impulse(EVENT))
            assert await local.receive() == Impulse(EVENT)


async def test_clean_eof_drains_events_before_detaching_publisher() -> None:
    sending, incoming = anyio.create_memory_object_stream[bytes](0)
    nerve = Nerve()
    topic = HabitatTopic(uuid7())
    fiber = StdioFiber(impulse_adapter, input=incoming)
    impulses = [
        Impulse(
            HabitatFailed(
                user_id=topic.user_id, habitat="local", sequence=i, message="failed"
            )
        )
        for i in range(3)
    ]
    async with sending, aclosing(fiber), nerve.subscribe(topic) as events:
        fiber.start()
        await sending.send(
            b"".join(impulse_adapter.dump_json(impulse) + b"\n" for impulse in impulses)
        )
        sending.close()
        async with nerve.publish(topic, fiber=fiber):
            await fiber.wait_closed()
        assert held(events) == impulses


async def test_cancelling_a_waiter_does_not_cancel_the_fiber() -> None:
    core = Nerve()
    topic = HabitatTopic(uuid7())
    async with connected(core, topic) as (core_fiber, guest):
        waiting = asyncio.create_task(core_fiber.wait_closed())
        await asyncio.sleep(0)
        waiting.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiting
        assert not core_fiber.closed
        async with core.subscribe(topic) as events:
            await guest.send(Impulse(EVENT))
            assert await events.receive() == Impulse(EVENT)


async def test_overflow_disconnects_peer_without_interrupting_local_publisher() -> None:
    nerve = Nerve()
    channel = ChannelTopic(HERE.channel_tentacle_id)
    outgoing, receiving = anyio.create_memory_object_stream[bytes](0)
    fiber = StdioFiber(impulse_adapter, output=outgoing)
    async with (
        receiving,
        aclosing(fiber),
        nerve.subscribe(channel, fiber=fiber),
        nerve.subscribe(channel) as local,
    ):
        fiber.start()
        async with asyncio.timeout(2), nerve.publish(HERE) as impulses:
            for _ in range(FIBER_BUFFER * 3):
                await impulses.send(Impulse(EVENT, HERE))
        assert len(held(local)) == FIBER_BUFFER * 3
        assert fiber.closed
        assert fiber.failure is not None
    assert not nerve.subscribers


async def test_immediate_close_releases_stdio_before_reader_task_starts() -> None:
    sending, incoming = anyio.create_memory_object_stream[bytes](0)
    nerve = Nerve()
    topic = HabitatTopic(uuid7())
    fiber = StdioFiber(impulse_adapter, input=incoming)
    async with sending, nerve.publish(topic, fiber=fiber):
        connection = fiber.start()
        await fiber.aclose()
        assert connection.cancelled()
        with pytest.raises(anyio.BrokenResourceError):
            await sending.send(b"unused")
    assert not nerve.subscribers
