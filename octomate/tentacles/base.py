from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Generic, TypeVar

if TYPE_CHECKING:
    from octomate.base import Octomate

TentaclePrimaryT = TypeVar("TentaclePrimaryT")
TentacleSecondaryT = TypeVar("TentacleSecondaryT")


@dataclass
class Tentacle(Generic[TentaclePrimaryT, TentacleSecondaryT]):
    """Base for external integrations managed by the Octomate host.

    A tentacle is a lifecycle component, not a message dispatcher. Channel
    tentacles receive platform events and call Octomate.awake; agent tentacles
    expose pydantic-ai-style run methods.
    """

    id: str
    octomate: Octomate = field(repr=False)

    async def activate(self) -> None:
        """Start any long-lived platform client owned by the tentacle."""

    async def deactivate(self) -> None:
        """Stop any long-lived platform client owned by the tentacle."""
