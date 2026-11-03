"""The nerve: what a thread does, as it happens, for whoever is watching.

Publish and subscribe, keyed by channel, thread, user or sandbox topics. The
nerve carries wire events and typed sandbox signals. Local publishers and
subscribers use memory fibers by default; supplied fibers use the same lifecycle.
Wire events are the run's stream as a wire consumer sees it, and what the graph says around a
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
quietly skipped ahead. Each local stream is a fiber: a pair of buffered ends in
memory. Publishing is one way and knows nothing of who subscribed, and
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
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass, replace
from typing import Annotated, Generic, Literal, overload

import anyio
from anyio.abc import ObjectReceiveStream, ObjectSendStream
from pydantic import (
    UUID7,
    ConfigDict,
    Discriminator,
    Tag,
    TypeAdapter,
    with_config,
)
from typing_extensions import TypeVar

from octomate.capabilities.harness.events import WireEvent
from octomate.fibers import FIBER_BUFFER as FIBER_BUFFER
from octomate.fibers import Fiber, FiberError, MemoryFiber
from octomate.schemas.conversation import ChannelAddress
from octomate.schemas.sandbox import SandboxAgentSignal, SandboxSignal
from octomate.schemas.thread import ThreadKey
from octomate.types.json import JsonValue

EventT = TypeVar("EventT", default=WireEvent, covariant=True)


# Python 3.12 needs typing_extensions for a type parameter with a default.
@with_config(
    ConfigDict(
        extra="forbid",
        ser_json_bytes="base64",
        val_json_bytes="base64",
    )
)
@dataclass(frozen=True)
class Impulse(Generic[EventT]):
    """One event or signal with an optional address and the routing metadata
    used by `Nerve.publish`. Direct topic publication carries it unchanged."""

    event: EventT
    address: ChannelAddress | None = None
    # Whether Nerve.publish excludes the origin thread for a private delivery.
    private: bool = False
    # The thread Nerve.publish stamps on the event: the asking thread
    # for a kick, the native thread for a tail, or None when neither names a thread.
    origin: ThreadKey | None = None
    invocation_id: UUID7 | None = None  # Correlates sandbox signals and run events.


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


@dataclass(frozen=True)
class SandboxTopic:
    """A user's sandbox lifecycle, or one agent invocation inside that sandbox."""

    user_id: UUID7
    invocation_id: UUID7 | None = None


type EventTopic = ChannelTopic | ThreadTopic | UserTopic
type Topic = EventTopic | SandboxTopic


def nerve_event_family(
    value: WireEvent | SandboxSignal | dict[str, JsonValue],
) -> Literal["event", "signal"] | None:
    """Keep malformed signals from matching events with default-valued fields."""
    if isinstance(value, dict):
        if "event_kind" in value:
            return "event"
        return "signal" if "kind" in value else None
    return "signal" if isinstance(value, SandboxAgentSignal) else "event"


type NerveEvent = Annotated[
    Annotated[WireEvent, Tag("event")] | Annotated[SandboxSignal, Tag("signal")],
    Discriminator(nerve_event_family),
]


impulse_adapter = TypeAdapter(Impulse[NerveEvent])


class Nerve:
    """Route impulses through publishers and subscribers backed by any fiber."""

    def __init__(self) -> None:
        # Each subscriber's fiber, by the topic it hears.
        self.subscribers: defaultdict[Topic, set[Fiber[Impulse[NerveEvent]]]] = (
            defaultdict(set)
        )

    @overload
    @asynccontextmanager
    def subscribe(
        self,
        topic: EventTopic,
        *,
        backlog: float = math.inf,
        fiber: Fiber[Impulse[NerveEvent]] | None = None,
    ) -> AsyncGenerator[ObjectReceiveStream[Impulse]]: ...

    @overload
    @asynccontextmanager
    def subscribe(
        self,
        topic: Topic,
        *,
        backlog: float = math.inf,
        fiber: Fiber[Impulse[NerveEvent]] | None = None,
    ) -> AsyncGenerator[ObjectReceiveStream[Impulse[NerveEvent]]]: ...

    @asynccontextmanager
    async def subscribe(
        self,
        topic: Topic,
        *,
        backlog: float = math.inf,
        fiber: Fiber[Impulse[NerveEvent]] | None = None,
    ) -> AsyncGenerator[ObjectReceiveStream[Impulse[NerveEvent]]]:
        """The receive end of a stream of the topic's impulses, from now until
        the block ends. Nothing is dropped: a reader that falls more than
        `backlog` impulses behind reads what was held for it, and then its
        stream ends.

        `ChannelTopic` names a channel's addresses, `ThreadTopic` wraps one
        `ThreadKey`, `UserTopic` wraps a registered user's UUID, and `SandboxTopic`
        names the sandbox lifecycle or a specific invocation's stream. Receivers select the
        messages they handle; the nerve does not distinguish commands from events.
        Publishers choose authorized topics before publishing; the nerve does
        not filter subscribers within a topic.

        A supplied fiber receives the same impulses as a local subscription.
        Its caller owns its buffering and closure; leaving only unsubscribes it.
        """
        subscriber = (
            fiber if fiber is not None else MemoryFiber[Impulse[NerveEvent]](backlog)
        )
        recipients = self.subscribers.get(topic, set())
        if subscriber.closed or subscriber in recipients:
            raise ValueError("Fiber is already subscribed or closed")
        self.subscribers[topic].add(subscriber)
        try:
            yield subscriber
        finally:
            self.subscribers[topic].discard(subscriber)
            if not self.subscribers[topic]:
                del self.subscribers[topic]
            if fiber is None:
                subscriber.close()

    @overload
    @asynccontextmanager
    def publish(
        self,
        origin: ChannelAddress | None,
        *,
        fiber: Fiber[Impulse[NerveEvent]] | None = None,
    ) -> AsyncGenerator[ObjectSendStream[Impulse]]: ...

    @overload
    @asynccontextmanager
    def publish(
        self,
        origin: Topic,
        *,
        fiber: Fiber[Impulse[NerveEvent]] | None = None,
    ) -> AsyncGenerator[ObjectSendStream[Impulse[NerveEvent]]]: ...

    @asynccontextmanager
    async def publish(
        self,
        origin: ChannelAddress | Topic | None,
        *,
        fiber: Fiber[Impulse[NerveEvent]] | None = None,
    ) -> AsyncGenerator[ObjectSendStream[Impulse[NerveEvent]]]:
        """A buffered publisher for kicks and native tails, open for the block.

        A kick names the thread it was asked on as `origin`; a native tail names
        its native thread and scopes the stream to its connection. The publisher
        stamps that origin on each impulse and publishes separately to its channel,
        its destination thread, and its origin thread unless private. Each distinct
        topic receives one publication.

        Sending may wait for the pump, never for a subscriber. Everything sent before
        the block ends is published; closing emits no lifecycle events of its own.

        A topic publishes impulses unchanged to that topic. A supplied fiber is
        read for the block's lifetime and remains owned by its caller. Its incoming
        impulses are delivered to subscribers on the core-bound topic.
        """
        publisher = fiber if fiber is not None else MemoryFiber[Impulse[NerveEvent]]()
        if publisher.closed:
            raise ValueError("Publisher fiber is closed")
        asked = (
            ThreadKey.from_address(origin)
            if isinstance(origin, ChannelAddress)
            else None
        )
        topic = (
            origin
            if isinstance(origin, ChannelTopic | ThreadTopic | UserTopic | SandboxTopic)
            else None
        )

        async def pump() -> None:
            try:
                async for sent in publisher:
                    if topic is not None:
                        self.deliver(topic, sent)
                        continue
                    impulse = replace(sent, origin=asked)
                    topics: set[Topic] = set()
                    if impulse.address is not None:
                        topics.add(ChannelTopic(impulse.address.channel_tentacle_id))
                        topics.add(ThreadTopic(ThreadKey.from_address(impulse.address)))
                    if not impulse.private and asked is not None:
                        topics.add(ThreadTopic(asked))
                    for destination in topics:
                        self.deliver(destination, impulse)
            except (anyio.BrokenResourceError, anyio.ClosedResourceError):
                pass  # The owner closed the supplied fiber while its pump was waiting.
            finally:
                if fiber is None:
                    publisher.close()

        pumping = asyncio.create_task(pump())
        try:
            yield publisher
        finally:
            if fiber is not None:
                if not publisher.closed or publisher.failure is not None:
                    pumping.cancel()
                with suppress(asyncio.CancelledError):
                    await pumping
            else:
                publisher.close_send()
                try:
                    await pumping
                finally:
                    publisher.close()

    def deliver(
        self,
        topic: Topic,
        impulse: Impulse[NerveEvent],
    ) -> None:
        """Fan out to every subscriber of one topic.

        A slow local subscriber is cut off at its backlog. A full or broken peer
        fiber is disconnected independently; other subscribers keep receiving.
        Message handling belongs to receivers; unobserved publications are dropped.
        """
        recipients = self.subscribers.get(topic, set())
        for fiber in recipients:
            if fiber.closed:
                continue
            try:
                fiber.send_nowait(impulse)
            except (
                anyio.WouldBlock,
                anyio.BrokenResourceError,
                anyio.ClosedResourceError,
            ):
                fiber.fail(FiberError("Subscriber fiber cannot accept publication"))
