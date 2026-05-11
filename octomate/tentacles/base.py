from __future__ import annotations

from abc import ABC
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from octomate.octopus import Octomate


class Tentacle(ABC):
    """Base for tentacles. Concrete shape (channel vs agent) lives in subclasses.

    `octopus` is bound by `Octomate.attach()` — NOT by the constructor.
    Subclasses can build themselves standalone (no orchestrator needed at
    construction); they only need the octopus reference once they're actually
    running (emit/kick/sinks all live behind methods that fire post-attach).
    Accessing `self.octopus` before attach raises `AttributeError` —
    intentionally loud, so a misuse fails fast.
    """

    id: str
    octopus: Octomate

    def __init__(self, id: str) -> None:
        self.id = id
