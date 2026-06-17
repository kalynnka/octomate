from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Generic, Self, TypeVar

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

    A tentacle is an async context manager: the host enters it to start the
    tentacle's long-lived resources and exits it to tear them down. The base
    implementation is a no-op; subclasses override `__aenter__`/`__aexit__`.
    """

    id: str
    octomate: Octomate = field(repr=False)

    async def __aenter__(self) -> Self:
        """Start any long-lived platform resources owned by the tentacle."""
        return self

    async def __aexit__(self, *exc: object) -> None:
        """Stop any long-lived platform resources owned by the tentacle."""
