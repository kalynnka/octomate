from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from collections import defaultdict
from typing import TYPE_CHECKING

import anyio

from octomate.schemas.session import SessionKey

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from anyio.abc import TaskGroup

    from octomate.nerve import OctopusNerve
    from octomate.schemas.adaptors import ActionUnion
    from octomate.schemas.events import MessageEvent

logger = logging.getLogger(__name__)


class BaseTentacle(ABC):
    nerve: OctopusNerve
    name: str
    self_id: int | None
    self_name: str | None

    def __init__(self, name: str) -> None:
        self.name = name
        self.self_id: int | None = None
        self.self_name: str | None = None

    @abstractmethod
    async def introspect(self) -> None:
        """Discover and set this tentacle's own identity (self_id, self_name, ...)."""

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
    _flush_delay: float
    _handler: Callable[[SessionKey, list[MessageEvent]], Awaitable[None]]
    _buckets: defaultdict[SessionKey, list[MessageEvent]]
    _pending: set[SessionKey]
    _tg: TaskGroup | None

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
