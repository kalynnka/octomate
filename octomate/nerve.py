"""The nerve: what a thread does, as it happens, for whoever is watching.

In-process publish and subscribe, keyed by the thread, carrying wire events:
the run's stream as a wire consumer sees it, and what the graph says around a
run — its start and end, a move, a failure, the reflex waking and falling
asleep, an authorization. Every way in and out is a stream end the nerve
hands over. A kick publishes on the send end of a stream opened on the thread
it was asked on, as impulses: an event and the thread it happens on. The
nerve pumps the other end, delivering each impulse to the subscribers of its
thread and of the asking thread, so a console follows the kick wherever a
move carries it. A subscriber reads the receive end of a stream of its own,
for as long as its block runs, as it would read the run itself: a channel
drives its timeline from it, and the console's live socket dumps each event
as it comes. Each such stream is a fiber: a pair of ends in memory today, and
the one thing to replace for a stream that outlives the process. Publishing
is one way and knows nothing of who subscribed. Nothing waits for a reader,
and nothing is kept: an event published to a thread nobody subscribes to goes
nowhere, a subscriber that has fallen behind misses what did not fit, and a
console re-reads the thread ledger when it opens.
"""

from __future__ import annotations

import asyncio
from collections import defaultdict
from collections.abc import AsyncGenerator, Generator
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass

import anyio
from anyio.abc import ObjectReceiveStream, ObjectSendStream
from anyio.streams.memory import MemoryObjectReceiveStream, MemoryObjectSendStream

from octomate.capabilities.harness.events import WireEvent
from octomate.schemas.thread import ThreadKey

# How far a fiber may run ahead of its reader: a publisher waits beyond it, a
# subscriber misses.
FIBER_BUFFER = 256


@dataclass(frozen=True)
class Impulse:
    """One event on its way through the nerve, and the thread it happens on;
    None for one that happens on no thread but the one the kick was asked on."""

    event: WireEvent
    key: ThreadKey | None = None
    # A delivery to a person's private surface, heard on that thread alone and
    # never on the thread the kick was asked on, which others may be watching.
    private: bool = False


class Fiber[ItemT]:
    """One stream through the nerve, both ends: the publisher's or a subscriber's."""

    send: MemoryObjectSendStream[ItemT]
    receive: MemoryObjectReceiveStream[ItemT]

    def __init__(self) -> None:
        self.send, self.receive = anyio.create_memory_object_stream[ItemT](FIBER_BUFFER)

    def close(self) -> None:
        self.send.close()
        self.receive.close()


class Nerve:
    """The subscribers of each thread, and what is published to them."""

    def __init__(self) -> None:
        # Each subscriber's fiber, by the thread's key.
        self.subscribers: defaultdict[ThreadKey, set[Fiber[WireEvent]]] = defaultdict(
            set
        )

    @contextmanager
    def subscribe(self, key: ThreadKey) -> Generator[ObjectReceiveStream[WireEvent]]:
        """The receive end of a stream of the thread's events, from now until
        the block ends."""
        fiber = Fiber[WireEvent]()
        self.subscribers[key].add(fiber)
        try:
            yield fiber.receive
        finally:
            self.subscribers[key].discard(fiber)
            if not self.subscribers[key]:
                del self.subscribers[key]
            fiber.close()

    @asynccontextmanager
    async def publish(
        self, origin: ThreadKey | None
    ) -> AsyncGenerator[ObjectSendStream[Impulse]]:
        """The send end of a kick's stream, open for the block: each impulse sent
        is heard on the thread it names and on `origin`, the thread the kick was
        asked on, or on `origin` alone when it names none, or on its own thread
        alone when it is private. Everything sent before the block ends is
        delivered."""
        fiber = Fiber[Impulse]()
        pump = asyncio.create_task(self.pump(fiber.receive, origin))
        try:
            yield fiber.send
        finally:
            fiber.send.close()
            await pump
            fiber.close()

    async def pump(
        self, impulses: ObjectReceiveStream[Impulse], origin: ThreadKey | None
    ) -> None:
        """Deliver each impulse until the stream ends."""
        async for impulse in impulses:
            heard = (impulse.key,) if impulse.private else (impulse.key, origin)
            for key in {one for one in heard if one is not None}:
                self.deliver(key, impulse.event)

    def deliver(self, key: ThreadKey, event: WireEvent) -> None:
        """One event to every subscriber of the thread, now, waiting for none."""
        for fiber in self.subscribers.get(key, ()):
            try:
                fiber.send.send_nowait(event)
            except (anyio.WouldBlock, anyio.BrokenResourceError):
                pass  # Fallen behind, or already leaving: either way, missed.
