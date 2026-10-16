"""A channel's siphon: where what the nerve carries leaves for the platform.

An octopus pushes its ink out through its siphon. A channel's siphon pushes
out what the nerve carries to it, through the channel's feelers — a run as a
timeline when the channel streams, or its sends, batches and reply when it
does not, and around runs a move's line, a failure, a send, an authorization.
It holds the channel's subscription to its own addresses, and draws each thread
in order in a lane of its own, so a slow thread holds up no other. A row the
graph recorded comes with its id, and is marked with the platform id it was
rendered as.
"""

from __future__ import annotations

import asyncio
import logging
import math
from collections.abc import AsyncGenerator
from contextlib import ExitStack, suppress
from typing import TYPE_CHECKING

import anyio
from anyio.abc import ObjectReceiveStream
from anyio.streams.memory import MemoryObjectReceiveStream, MemoryObjectSendStream
from pydantic import UUID7

from octomate.capabilities.harness.events import (
    ActionBatchEvent,
    ArtifactAvailableEvent,
    GatewayEvent,
    MessageSentEvent,
    OAuthAuthorizationEvent,
    RunEndedEvent,
    RunErrorEvent,
    RunResultEvent,
    RunStartedEvent,
    WireEvent,
)
from octomate.nerve import ChannelTopic, Impulse
from octomate.schemas.conversation import ChannelAddress
from octomate.schemas.segments import MessageSegment
from octomate.schemas.thread import ThreadKey
from octomate.tentacles.feelers.output import IMMessageID

if TYPE_CHECKING:
    from octomate.tentacles.channel import ChannelTentacle

logger = logging.getLogger(__name__)


class Siphon:
    """Draws what the nerve carries to one channel, thread by thread."""

    def __init__(self, channel: ChannelTentacle) -> None:
        self.channel = channel
        # Each thread's lane: the impulses it has yet to draw.
        self.lanes: dict[ThreadKey, MemoryObjectSendStream[Impulse]] = {}
        self.subscription = ExitStack()
        self.reading: asyncio.Task[None] | None = None

    def start(self) -> None:
        """Subscribe to the channel's addresses now, so nothing published from
        here on is missed, and read in the background until `stop`."""
        nerve = self.channel.octomate.nerve
        impulses = self.subscription.enter_context(
            nerve.subscribe(ChannelTopic(self.channel.id))
        )
        self.reading = asyncio.create_task(self.read(impulses))

    async def stop(self) -> None:
        if self.reading is not None:
            self.reading.cancel()
            with suppress(asyncio.CancelledError):
                await self.reading
        self.subscription.close()

    async def read(self, impulses: ObjectReceiveStream[Impulse]) -> None:
        """Hand each impulse to its thread's lane, opening one where none is."""
        async with asyncio.TaskGroup() as lanes:
            async for impulse in impulses:
                if impulse.address is None:
                    continue  # The nerve hands a channel only what has an address.
                key = ThreadKey.from_address(impulse.address)
                lane = self.lanes.get(key)
                if lane is None:
                    lane, waiting = anyio.create_memory_object_stream[Impulse](math.inf)
                    self.lanes[key] = lane
                    lanes.create_task(self.draw(key, waiting))
                lane.send_nowait(impulse)

    async def draw(
        self, key: ThreadKey, impulses: MemoryObjectReceiveStream[Impulse]
    ) -> None:
        """Draw one thread's impulses in order, and close the lane once nothing
        is left in it. A rendering that fails is logged and never ends the lane."""
        with impulses:
            async for impulse in impulses:
                try:
                    await self.feel(impulse, impulses)
                except Exception:
                    logger.warning(
                        "Channel %s: rendering %s failed",
                        self.channel.id,
                        type(impulse.event).__name__,
                        exc_info=True,
                    )
                if impulses.statistics().current_buffer_used == 0:
                    self.lanes.pop(key).close()
                    return

    async def feel(
        self, impulse: Impulse, impulses: MemoryObjectReceiveStream[Impulse]
    ) -> None:
        """Draw one impulse; a run's start draws the whole run, through its end."""
        feelers = self.channel.feelers
        address = impulse.address
        if address is None:
            return
        match impulse.event:
            case RunStartedEvent():
                await self.run(address, impulses)
            case GatewayEvent() as event:
                line = await feelers.present(address, event)
                await self.mark(event.thread_message_id, line)
            case RunErrorEvent() as event:
                await feelers.present(address, event)
            case MessageSentEvent() as event:
                sent = await feelers.segments.present(
                    event.destination or address, event.segments
                )
                await self.mark(event.thread_message_id, sent)
            case OAuthAuthorizationEvent() as event:
                await feelers.oauth.present(address, event)
            case ArtifactAvailableEvent() as event:
                await feelers.artifacts.present(address, event)
            case _:
                pass

    async def run(
        self, address: ChannelAddress, impulses: MemoryObjectReceiveStream[Impulse]
    ) -> None:
        """Draw one agent run from the lane, up to its end: as a timeline when the
        channel streams, else quietly. What a failed rendering left unread is
        drained, so the lane resumes after the run."""
        ended: list[RunEndedEvent] = []

        async def events() -> AsyncGenerator[WireEvent]:
            async for impulse in impulses:
                if isinstance(impulse.event, RunEndedEvent):
                    ended.append(impulse.event)
                    return
                yield impulse.event

        stream = events()
        try:
            if self.channel.config.stream.enabled:
                async with self.channel.feelers.timeline.open(address) as timeline:
                    timeline.artifacts = self.channel.feelers.artifacts
                    await timeline.drive(stream)
                rendered = timeline.message_id
            else:
                rendered = await self.quietly(address, stream)
        except Exception:
            async for _ in stream:
                pass
            raise
        if ended:
            await self.mark(ended[0].thread_message_id, rendered)

    async def quietly(
        self, address: ChannelAddress, events: AsyncGenerator[WireEvent]
    ) -> IMMessageID | None:
        """A run on a channel that does not stream: its sends, batches and
        authorizations as they come, then its reply once. Answers the platform id
        the reply was posted as."""
        feelers = self.channel.feelers
        output: str | list[MessageSegment] | None = None
        async for event in events:
            match event:
                case MessageSentEvent():
                    await feelers.segments.present(
                        event.destination or address, event.segments
                    )
                case ActionBatchEvent():
                    await feelers.present_actions(
                        address,
                        event,
                        action_manager=self.channel.octomate.deferred_actions,
                    )
                case OAuthAuthorizationEvent():
                    await feelers.oauth.present(address, event)
                case ArtifactAvailableEvent():
                    await feelers.artifacts.present(address, event)
                case RunResultEvent():
                    output = event.output
                case _:
                    pass
        if isinstance(output, str):
            return await feelers.markdown.present(address, output) if output else None
        return await feelers.segments.present(address, output) if output else None

    async def mark(
        self, thread_message_id: UUID7 | None, platform_message_id: str | None
    ) -> None:
        """Name the platform message a recorded row was rendered as."""
        if thread_message_id is not None:
            await self.channel.octomate.threads.mark_presented(
                thread_message_id, platform_message_id
            )
