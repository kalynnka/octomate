"""Human deferral and stream interjections shared by agent providers."""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncGenerator, AsyncIterable
from dataclasses import dataclass
from typing import Protocol

from pydantic import UUID7
from pydantic_ai.tools import DeferredToolRequests

from octomate.events import ActionBatchAnsweredEvent, ActionBatchEvent


class DeferredSuspender(Protocol):
    """Persist actions for a human response. The caller controls the run's
    lifecycle: `request` lets it continue, `pause` keeps it waiting for a live
    reply, and `suspend` ends it for later continuation.

    Returning an `ActionBatchEvent` asks the react loop to put the batch *on the
    stream*, for whoever draws the run to present; returning `None` means there
    is nothing to present, as for a deferral the graph performs itself.
    """

    async def suspend(self, requests: DeferredToolRequests) -> ActionBatchEvent | None:
        """Persist actions for a run that ends until its deferral is resolved.

        The caller ends the current run; resolution starts a later run with the
        collected results. Return None for a deferral handled by the graph itself.
        """
        ...

    async def pause(
        self, requests: DeferredToolRequests, *, batch_id: UUID7
    ) -> ActionBatchEvent:
        """Persist actions whose response unblocks the same live run.

        The caller registers a waiter for batch_id before presenting the event,
        then awaits the response within the current run. This method does not wait.
        """
        ...

    async def request(self, requests: DeferredToolRequests) -> ActionBatchEvent:
        """Persist actions while the current run continues without waiting.

        The caller presents the event and keeps running. The completed response
        starts a later turn after the current run finishes.
        """
        ...


@dataclass(frozen=True)
class Read[T]:
    """One item of a runtime's own stream."""

    item: T


@dataclass(frozen=True)
class Ended:
    """A runtime's own stream is over, or failed with `error`."""

    error: Exception | None = None


# What a live run's callbacks put on its stream: the batch an approval pauses the
# run on, and its answer once the run takes it.
type Interjection = ActionBatchEvent | ActionBatchAnsweredEvent


class Interjections[T]:
    """What a live run's callbacks put on its stream while it waits on the runtime:
    the batch an approval pauses the run on, and the answer it resumes on, for
    whoever draws the run.

    The runtime's own stream is read in a task of its own, so a batch gets through
    while the runtime is still waiting for its answer."""

    def __init__(self) -> None:
        self.queue: asyncio.Queue[Read[T] | Interjection | Ended] = asyncio.Queue()

    def interject(self, event: Interjection) -> None:
        self.queue.put_nowait(event)

    async def around(
        self, source: AsyncIterable[T]
    ) -> AsyncGenerator[T | Interjection]:
        """`source`'s items in order, with each interjection as it is made."""
        reading = asyncio.create_task(self.read(source))
        try:
            while not isinstance(entry := await self.queue.get(), Ended):
                yield entry.item if isinstance(entry, Read) else entry
            if entry.error is not None:
                raise entry.error
        finally:
            reading.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await reading

    async def read(self, source: AsyncIterable[T]) -> None:
        try:
            async for item in source:
                self.queue.put_nowait(Read(item))
        except Exception as error:
            self.queue.put_nowait(Ended(error))
        else:
            self.queue.put_nowait(Ended())
