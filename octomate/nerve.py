"""The nerve: what a thread does, as it happens, for whoever is watching.

In-process publish and subscribe, keyed by channel, thread or user topics, carrying
wire events:
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
stream of its own, for as long as its block runs, and one that falls further
behind than its backlog reads what was held for it and is cut off, never
quietly skipped ahead. Each such stream is a fiber: a pair of ends in
memory today, and the one thing to replace for a stream that outlives the
process. Publishing is one way and knows nothing of who subscribed, and
nothing is kept for a reader that is not there: an event published to a
thread nobody subscribes to goes nowhere, and a console re-reads the thread
ledger when it opens.

`Nerve.deliver` reaches every subscriber of exactly one topic. A publisher sends
user notifications by delivering separately to each authorized user's topic.
`Nerve.publish` owns channel and thread routing for kicks and native tails.
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
from pydantic import UUID7

from octomate.capabilities.harness.events import WireEvent
from octomate.schemas.conversation import ChannelAddress
from octomate.schemas.thread import ThreadKey

# How far a publisher's fiber may run ahead of the pump before it waits.
FIBER_BUFFER = 1024


@dataclass(frozen=True)
class Impulse:
    """One wire event with an optional channel address and the routing metadata
    used by `Nerve.publish`. Direct topic publication carries it unchanged."""

    event: WireEvent
    address: ChannelAddress | None = None
    # Whether Nerve.publish excludes the origin thread for a private delivery.
    private: bool = False
    # The thread Nerve.publish stamps on the event: the asking thread
    # for a kick, the native thread for a tail, or None when neither names a thread.
    origin: ThreadKey | None = None


@dataclass(frozen=True)
class ChannelTopic:
    """Every impulse at one channel's addresses, each once."""

    channel_tentacle_id: str


@dataclass(frozen=True)
class ThreadTopic:
    """The impulses published to one thread."""

    key: ThreadKey


@dataclass(frozen=True)
class UserTopic:
    """Notifications explicitly published to one registered user."""

    user_id: UUID7


type Topic = ChannelTopic | ThreadTopic | UserTopic


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
    """The subscribers of each channel, thread and user, and what is
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
        stream ends.

        `ChannelTopic` names a channel's addresses, `ThreadTopic` wraps one
        `ThreadKey`, and `UserTopic` wraps a registered user's UUID.
        Publishers choose authorized topics before publishing; the nerve does
        not filter subscribers within a topic.
        """
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
        """A buffered publisher for kicks and native tails, open for the block.

        A kick names the thread it was asked on as `origin`; a native tail names
        its native thread and scopes the stream to its connection. The publisher
        stamps that origin on each impulse and publishes separately to its channel,
        its destination thread, and its origin thread unless private. Each distinct
        topic receives one publication.

        Sending may wait for the pump, never for a subscriber. Everything sent before
        the block ends is published; closing emits no lifecycle events of its own.
        """
        fiber = Fiber[Impulse]()
        asked = ThreadKey.from_address(origin) if origin is not None else None

        async def pump() -> None:
            async for sent in fiber.receive:
                impulse = replace(sent, origin=asked)
                topics: set[Topic] = set()
                if impulse.address is not None:
                    topics.add(ChannelTopic(impulse.address.channel_tentacle_id))
                    topics.add(ThreadTopic(ThreadKey.from_address(impulse.address)))
                if not impulse.private and asked is not None:
                    topics.add(ThreadTopic(asked))
                for topic in topics:
                    self.deliver(topic, impulse)

        pumping = asyncio.create_task(pump())
        try:
            yield fiber.send
        finally:
            fiber.send.close()
            await pumping
            fiber.close()

    def deliver(self, topic: Topic, impulse: Impulse) -> None:
        """Deliver one impulse to every subscriber of exactly `topic`, waiting
        for none. The impulse is delivered unchanged; its address, origin and
        privacy flag do not select or exclude subscribers."""
        for fiber in self.subscribers.get(topic, set()):
            try:
                fiber.send.send_nowait(impulse)
            except anyio.WouldBlock:
                # Further behind than it asked to be held for: its stream ends.
                fiber.send.close()
            except (anyio.BrokenResourceError, anyio.ClosedResourceError):
                pass  # Already leaving, or already cut off.
