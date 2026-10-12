"""The nerve: what a thread does, as it happens, for whoever is watching.

In-process publish and subscribe, keyed by the thread, carrying wire events:
the run's stream as a wire consumer sees it, and what the graph says around a
run — its start and end, a move, a failure, the reflex waking and falling
asleep, an authorization. Every way in and out is a stream end the nerve
hands over. A kick publishes on the send end of a stream opened on the thread
it was asked on, as impulses: an event and the address it happens at. The
nerve pumps the other end, delivering each impulse to the channel it happens
on and to the subscribers of its thread and of the asking thread, so a
console follows the kick wherever a move carries it. A channel holds one
subscription for all its threads and draws every run on them; it misses
nothing and holds nobody up, since what it has yet to read is kept for it. A
thread's subscriber — the console's live socket — reads the receive end of a
stream of its own, for as long as its block runs, and one that falls behind
misses what did not fit. Each such stream is a fiber: a pair of ends in
memory today, and the one thing to replace for a stream that outlives the
process. Publishing is one way and knows nothing of who subscribed, and
nothing is kept for a reader that is not there: an event published to a
thread nobody subscribes to goes nowhere, and a console re-reads the thread
ledger when it opens.
"""

from __future__ import annotations

import asyncio
import math
from collections import defaultdict
from collections.abc import AsyncGenerator, Generator
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass, replace

import anyio
from anyio.abc import ObjectReceiveStream, ObjectSendStream
from anyio.streams.memory import MemoryObjectReceiveStream, MemoryObjectSendStream

from octomate.capabilities.harness.events import WireEvent
from octomate.schemas.conversation import ChannelAddress
from octomate.schemas.thread import ThreadKey

# How far a kick's own fiber may run ahead of the pump before the kick waits.
FIBER_BUFFER = 256


@dataclass(frozen=True)
class Impulse:
    """One event on its way through the nerve, and the address it happens at;
    None for one that happens nowhere but the thread the kick was asked on."""

    event: WireEvent
    address: ChannelAddress | None = None
    # A delivery to a person's private surface, heard on that thread alone and
    # never on the thread the kick was asked on, which others may be watching.
    private: bool = False
    # The thread the kick was asked on, which the nerve stamps as it pumps; None
    # outside a kick, or for a kick that names no thread.
    origin: ThreadKey | None = None


@dataclass(frozen=True)
class ChannelTopic:
    """Every impulse at one channel's addresses, each once."""

    channel_tentacle_id: str


# What a reader subscribes to: a channel's addresses, or one thread — that
# thread's own impulses and those of kicks asked on it, unless private.
type Topic = ChannelTopic | ThreadKey


class Fiber[ItemT]:
    """One stream through the nerve, both ends: a kick's or a subscriber's,
    holding up to `buffer` items its reader has yet to take."""

    send: MemoryObjectSendStream[ItemT]
    receive: MemoryObjectReceiveStream[ItemT]

    def __init__(self, buffer: float = FIBER_BUFFER) -> None:
        self.send, self.receive = anyio.create_memory_object_stream[ItemT](buffer)

    def close(self) -> None:
        self.send.close()
        self.receive.close()


class Nerve:
    """The subscribers of each channel and of each thread, and what is
    published to them."""

    def __init__(self) -> None:
        # Each subscriber's fiber, by the topic it hears.
        self.subscribers: defaultdict[Topic, set[Fiber[Impulse]]] = defaultdict(set)

    @contextmanager
    def subscribe(
        self, topic: Topic, *, backlog: float = math.inf
    ) -> Generator[ObjectReceiveStream[Impulse]]:
        """The receive end of a stream of the topic's impulses, from now until
        the block ends. Nothing is dropped: a reader that falls more than
        `backlog` impulses behind reads what was held for it, and then its
        stream ends."""
        fiber = Fiber[Impulse](backlog)
        self.subscribers[topic].add(fiber)
        try:
            yield fiber.receive
        finally:
            self.subscribers[topic].discard(fiber)
            if not self.subscribers[topic]:
                del self.subscribers[topic]
            fiber.close()

    @asynccontextmanager
    async def publish(
        self, origin: ChannelAddress | None
    ) -> AsyncGenerator[ObjectSendStream[Impulse]]:
        """The send end of a kick's stream, open for the block, asked on the
        thread at `origin`. Each impulse sent is heard at the address it names,
        and on the asking thread too unless it is private. Everything sent before
        the block ends is delivered."""
        fiber = Fiber[Impulse]()
        asked = ThreadKey.from_address(origin) if origin is not None else None

        async def pump() -> None:
            async for impulse in fiber.receive:
                self.deliver(replace(impulse, origin=asked))

        pumping = asyncio.create_task(pump())
        try:
            yield fiber.send
        finally:
            fiber.send.close()
            await pumping
            fiber.close()

    def deliver(self, impulse: Impulse) -> None:
        """One impulse, now, waiting for none: to the subscribers of its channel,
        of its thread, and of its asking thread unless it is private."""
        address = impulse.address
        topics: list[Topic] = []
        if address is not None:
            topics += [
                ChannelTopic(address.channel_tentacle_id),
                ThreadKey.from_address(address),
            ]
        if not impulse.private and impulse.origin is not None:
            topics.append(impulse.origin)
        fibers = set[Fiber[Impulse]]().union(
            *(self.subscribers.get(topic, set()) for topic in topics)
        )
        for fiber in fibers:
            try:
                fiber.send.send_nowait(impulse)
            except anyio.WouldBlock:
                # Further behind than it asked to be held for: its stream ends.
                fiber.send.close()
            except (anyio.BrokenResourceError, anyio.ClosedResourceError):
                pass  # Already leaving, or already cut off.
