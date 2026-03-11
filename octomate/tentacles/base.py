from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from collections import defaultdict
from typing import TYPE_CHECKING

import anyio

from octomate.schemas.events import SessionKey

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from anyio.abc import TaskGroup

    from octomate.nerve import OctopusNerve
    from octomate.schemas.adaptors import ActionUnion
    from octomate.schemas.events import MessageEvent

logger = logging.getLogger(__name__)


class BaseTentacle(ABC):
    nerve: OctopusNerve

    def __init__(self, name: str) -> None:
        self.name = name

    @abstractmethod
    async def activate(self) -> None:
        """Start the tentacle and begin receiving events."""

    @abstractmethod
    async def deactivate(self) -> None:
        """Stop the tentacle and release resources."""

    @abstractmethod
    async def act(self, action: ActionUnion) -> None:
        """Send an outbound action through this tentacle."""


class MessageBuffer:
    def __init__(
        self,
        flush_delay: float,
        handler: Callable[[SessionKey, list[MessageEvent]], Awaitable[None]],
    ) -> None:
        self._flush_delay = flush_delay
        self._handler = handler
        self._buckets: defaultdict[SessionKey, list[MessageEvent]] = defaultdict(list)
        self._pending: set[SessionKey] = set()
        self._tg: TaskGroup | None = None

    def bind(self, tg: TaskGroup) -> None:
        self._tg = tg

    def push(self, event: MessageEvent) -> None:
        key = event.session_key
        self._buckets[key].append(event)
        if key not in self._pending:
            self._pending.add(key)
            if self._tg is None:
                raise RuntimeError("MessageBuffer.bind() must be called before push()")
            self._tg.start_soon(self._flush_after_delay, key)

    async def _flush_after_delay(self, key: SessionKey) -> None:
        await anyio.sleep(self._flush_delay)
        self._pending.discard(key)
        batch = self._buckets.pop(key, [])
        if not batch:
            return
        try:
            await self._handler(key, batch)
        except Exception:
            logger.exception("Error handling batch for %s", key)
