"""Shared habitat lifecycle and managed runtime capabilities."""

from abc import ABC, abstractmethod
from contextlib import AbstractAsyncContextManager
from pathlib import Path, PurePosixPath

from anyio.abc import ByteStream
from pydantic import UUID7


class HabitatError(RuntimeError):
    """An actionable failure safe to publish without guest or credential output."""


class Habitat(ABC):
    """The execution environment core prepares for an agent invocation."""

    @property
    @abstractmethod
    def name(self) -> str:
        """Stable identifier of this habitat."""

    @abstractmethod
    def path(self, path: Path, *, root: Path | None) -> Path | PurePosixPath:
        """Translate a host path for execution, with the owner's host root.

        Native execution permits an unregistered workspace with no root.
        Managed runtimes must reject paths outside the supplied root.
        """

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
