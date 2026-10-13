"""The nerve in a test: a thread's live events as the console reads them, and a
channel's siphon drawing what a kick published, to the end."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

import anyio
from anyio.abc import ObjectReceiveStream
from anyio.streams.memory import MemoryObjectReceiveStream, MemoryObjectSendStream

from octomate import Octomate
from octomate.capabilities.harness.events import (
    ReflexAsleepEvent,
    wire_event_adapter,
)
from octomate.nerve import ChannelTopic, Impulse
from octomate.reflex.graph import ReflexGraphResult
from octomate.schemas.awakes import AwakeSignal
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


def held(live: ObjectReceiveStream[Impulse]) -> list[Impulse]:
    """Everything the nerve has handed `live` that it has yet to read, taken now."""
    # The nerve's fibers are memory streams, which can say what they hold.
    assert isinstance(live, MemoryObjectReceiveStream)
    return [live.receive_nowait() for _ in range(live.statistics().current_buffer_used)]


@asynccontextmanager
async def drawing(*channels: ChannelTentacle) -> AsyncGenerator[None]:
    """The channels' siphons started, as entering a channel starts its own. Leaving
    ends each one's stream on its host's nerve, and returns once every siphon has
    drawn everything it heard — what a kick published is on the page by then."""
    for channel in channels:
        channel.siphon.start()
    try:
        yield
    finally:
        for channel in channels:
            topic = ChannelTopic(channel.id)
            for fiber in channel.octomate.nerve.subscribers.get(topic, set()):
                fiber.send.close()
            if channel.siphon.reading is not None:
                await channel.siphon.reading
            await channel.siphon.stop()


async def drained(*channels: ChannelTentacle) -> None:
    """Returns once each started channel's siphon has drawn everything handed to
    it so far."""
    with anyio.fail_after(5):
        # A started siphon tells no one it is done; what it still holds shows it.
        while any(  # noqa: ASYNC110
            channel.siphon.lanes
            or any(
                fiber.receive.statistics().current_buffer_used
                for fiber in channel.octomate.nerve.subscribers.get(
                    ChannelTopic(channel.id), set()
                )
            )
            for channel in channels
        ):
            await asyncio.sleep(0)


async def kicked(octomate: Octomate, signal: AwakeSignal) -> ReflexGraphResult | None:
    """`octomate.kick` on a host whose channels were never entered, returning once
    each has drawn what the kick published."""
    async with drawing(*octomate.channels.values()):
        return await octomate.kick(signal)


def unwired() -> MemoryObjectSendStream[Impulse]:
    """A send end for deps built ahead of the kick that opens the real one: it
    refuses anything sent on it."""
    send, receive = anyio.create_memory_object_stream[Impulse]()
    receive.close()
    return send
