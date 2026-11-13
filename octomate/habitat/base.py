"""Shared habitat lifecycle and managed runtime capabilities."""

from abc import ABC, abstractmethod
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from anyio.abc import ByteStream
from pydantic import UUID7

from octomate.schemas.volumes.storage import Storage


class HabitatError(RuntimeError):
    """An actionable failure safe to publish without guest or credential output."""


@dataclass(frozen=True)
class Attachment:
    """A storage location's destination and requested access in a habitat.

    This value describes the mapping; the habitat provider makes it accessible.
    Native execution uses the existing host path as its destination.
    """

    storage: Storage
    destination: Path | PurePosixPath  # Absolute path visible to the agent harness.
    read_only: bool = False  # Access requested from the habitat provider.

    def __post_init__(self) -> None:
        if not self.storage.path.is_absolute() or not self.destination.is_absolute():
            raise HabitatError("Attachment paths must be absolute")
        if ".." in self.storage.path.parts or ".." in self.destination.parts:
            raise HabitatError("Attachment paths cannot contain parent traversal")

    def path(self, path: Path) -> Path | PurePosixPath:
        """Map a host path within this storage without consulting the filesystem."""
        if not path.is_absolute():
            raise HabitatError("Attachment paths must be absolute")
        if ".." in path.parts:
            raise HabitatError("Attachment paths cannot contain parent traversal")
        try:
            relative = path.relative_to(self.storage.path)
        except ValueError:
            raise HabitatError("Host path is outside the attached storage") from None
        return self.destination.joinpath(*relative.parts)


class Habitat(ABC):
    """The execution environment core prepares for an agent invocation."""

    @property
    @abstractmethod
    def name(self) -> str:
        """Stable identifier of this habitat."""

    @abstractmethod
    async def prepare(self) -> None:
        """Make the environment ready before an adapter runs in it."""

    @abstractmethod
    async def stop(self) -> None:
        """Release the environment's runtime resources, preserving persistent data."""


class ManagedHabitat(Habitat):
    """One registered user's persistent runtime, hosting agent processes."""

    user_id: UUID7  # The registered user whose private runtime this is.

    @property
    @abstractmethod
    def idle_timeout(self) -> float:
        """Seconds to keep the runtime after its last invocation ends."""

    @abstractmethod
    def execute(
        self, *command: str, workdir: str, env: dict[str, str] | None = None
    ) -> AbstractAsyncContextManager[ByteStream]:
        """Run a command with duplex stdio, closing and reaping it on exit."""
