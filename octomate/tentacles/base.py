"""Abstract base class for all tentacles (channel adapters)."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from octomate.nerve import ActionUnion, Nerve
    from octomate.schemas.events import OneBotEventUnion


class BaseTentacle(ABC):
    """A tentacle bridges a single external connection to the Nerve.

    Subclasses must implement :meth:`start`, :meth:`stop`, and
    :meth:`send`.  The helper :meth:`_push_event` stamps the
    ``tentacle_id`` on the event and publishes it to the inbound
    stream of the Nerve.
    """

    nerve: Nerve

    def __init__(self, name: str) -> None:
        self.name = name

    @abstractmethod
    async def start(self) -> None:
        """Connect to the external service and begin receiving events."""

    @abstractmethod
    async def stop(self) -> None:
        """Gracefully disconnect and release resources."""

    @abstractmethod
    async def send(self, action: ActionUnion) -> None:
        """Deliver an outbound action over this tentacle's connection."""

    async def _push_event(self, event: OneBotEventUnion) -> None:
        """Stamp ``tentacle_id`` and publish to the Nerve inbound stream."""
        event.tentacle_id = self.name  # type: ignore[union-attr]
        await self.nerve.publish_inbound(event)
