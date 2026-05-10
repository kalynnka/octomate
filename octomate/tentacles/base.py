from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Protocol, runtime_checkable

from octomate.schemas.events import MessageEvent
from octomate.schemas.conversation import ConversationKey


@runtime_checkable
class Octopus(Protocol):
    """Minimal orchestrator surface that Tentacles depend on.

    Concrete Octopus (with agent dispatch, sink registry, etc.) lands later.
    """

    async def kick(self, key: ConversationKey, contents: list[MessageEvent]) -> None: ...


class Tentacle(ABC):
    """Base for all tentacles — any bidirectional message channel."""

    id: str
    octopus: Octopus

    def __init__(self, id: str, octopus: Octopus) -> None:
        self.id = id
        self.octopus = octopus

    @abstractmethod
    async def __call__(
        self, key: ConversationKey, contents: list[MessageEvent]
    ) -> None: ...
