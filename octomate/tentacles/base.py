from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from typing import TYPE_CHECKING

from octomate.schemas.events import MessageEvent
from octomate.schemas.session import SessionKey

if TYPE_CHECKING:
    from octomate.octopus import Octopus

logger = logging.getLogger(__name__)


class Tentacle(ABC):
    """Base for all tentacles — any bidirectional message channel."""

    id: str
    octopus: Octopus

    def __init__(self, id: str, octopus: Octopus) -> None:
        self.id = id
        self.octopus = octopus

    @abstractmethod
    async def __call__(
        self,
        key: SessionKey,
        contents: list[MessageEvent],
    ): ...
