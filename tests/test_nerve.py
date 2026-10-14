"""What is published reaches the subscribers of its channel, its thread and the
thread the kick was asked on, each once, and waits for none."""

from __future__ import annotations

import anyio
import pytest
from anyio.streams.memory import MemoryObjectSendStream
from pydantic_ai.messages import PartDeltaEvent, TextPartDelta

from octomate.capabilities.harness.events import MessageSentEvent
from octomate.nerve import ChannelTopic, Impulse, Nerve
from octomate.schemas.conversation import ChannelAddress
from octomate.schemas.thread import ThreadKey
from tests.support.nerve import held

HERE = ChannelAddress("web", "thread", "lu", "lu", channel_thread_id="t1")
THERE = ChannelAddress("web", "thread", "lu", "lu", channel_thread_id="t2")
FAR = ChannelAddress("slack", "dm", "D1", "U1")
EVENT = PartDeltaEvent(index=0, delta=TextPartDelta(content_delta="hi"))


def test_an_impulse_reaches_its_channel_and_its_thread() -> None:
    nerve = Nerve()
    with (
        nerve.subscribe(ChannelTopic("web")) as channel,
        nerve.subscribe(ThreadKey.from_address(HERE)) as here,
        nerve.subscribe(ThreadKey.from_address(THERE)) as there,
    ):
        nerve.deliver(Impulse(EVENT, HERE))

        assert held(channel) == [Impulse(EVENT, HERE)]
        assert held(here) == [Impulse(EVENT, HERE)]
        assert held(there) == []
    assert nerve.subscribers == {}


def test_an_impulse_on_the_asking_thread_is_heard_there_once() -> None:
    nerve = Nerve()
    asked = ThreadKey.from_address(HERE)
    with nerve.subscribe(asked) as here:
        nerve.deliver(Impulse(EVENT, HERE, origin=asked))

        assert held(here) == [Impulse(EVENT, HERE, origin=asked)]


async def test_the_asking_thread_hears_a_kick_wherever_it_goes() -> None:
    nerve = Nerve()
    asked = ThreadKey.from_address(HERE)
    with (
        nerve.subscribe(asked) as asking,
        nerve.subscribe(ChannelTopic("slack")) as far,
    ):
        async with nerve.publish(HERE) as impulses:
            await impulses.send(Impulse(EVENT, FAR))
            await impulses.send(Impulse(EVENT))

        moved = Impulse(EVENT, FAR, origin=asked)
        assert held(asking) == [moved, Impulse(EVENT, origin=asked)]
        # A channel hears only what happens at its addresses.
        assert held(far) == [moved]


async def test_a_private_impulse_is_heard_only_where_it_lands() -> None:
    nerve = Nerve()
    sent = Impulse(MessageSentEvent(segments=[]), FAR, private=True)
    with (
        nerve.subscribe(ThreadKey.from_address(HERE)) as asking,
        nerve.subscribe(ThreadKey.from_address(FAR)) as dm,
    ):
        async with nerve.publish(HERE) as impulses:
            await impulses.send(sent)

        assert [impulse.event for impulse in held(dm)] == [sent.event]
        assert held(asking) == []


async def test_a_reader_past_its_backlog_reads_what_was_held_and_ends() -> None:
    nerve = Nerve()
    with nerve.subscribe(ThreadKey.from_address(HERE), backlog=2) as behind:
        for _ in range(3):
            nerve.deliver(Impulse(EVENT, HERE))

        assert [impulse async for impulse in behind] == [Impulse(EVENT, HERE)] * 2


def test_delivering_where_nobody_subscribes_is_nothing() -> None:
    Nerve().deliver(Impulse(EVENT, HERE))


async def test_publisher_buffers_1024_and_drains_without_waiting_for_readers() -> None:
    nerve = Nerve()
    sent = [
        Impulse(PartDeltaEvent(index=index, delta=TextPartDelta(str(index))), HERE)
        for index in range(1025)
    ]
    with (
        nerve.subscribe(ThreadKey.from_address(HERE)) as here,
        nerve.subscribe(ChannelTopic("web"), backlog=2) as behind,
    ):
        async with nerve.publish(None) as impulses:
            assert isinstance(impulses, MemoryObjectSendStream)
            for impulse in sent[:-1]:
                impulses.send_nowait(impulse)
            with pytest.raises(anyio.WouldBlock):
                impulses.send_nowait(sent[-1])
            await impulses.send(sent[-1])

        assert held(here) == sent
        assert [impulse async for impulse in behind] == sent[:2]
        with pytest.raises(anyio.ClosedResourceError):
            await impulses.send(sent[-1])
