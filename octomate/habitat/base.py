"""The base class every habitat provider subclasses."""

from abc import ABC, abstractmethod
from contextlib import AbstractAsyncContextManager

from anyio.abc import ByteStream


class HabitatError(RuntimeError):
    """An actionable failure safe to publish without guest or credential output."""


class Habitat(ABC):
    """One user's isolated, durable agent runtime; a sandbox is one kind."""

    @property
    @abstractmethod
    def name(self) -> str:
        """Stable identifier of this habitat."""

    @property
    @abstractmethod
    def idle_timeout(self) -> float:
        """Seconds to keep the runtime after its last invocation ends."""

    @abstractmethod
    async def prepare(self) -> None:
        """Create or reuse, start, and validate the runtime before returning."""

    @abstractmethod
    def execute(
        self, *command: str, workdir: str, env: dict[str, str] | None = None
    ) -> AbstractAsyncContextManager[ByteStream]:
        """Run a command with duplex stdio, closing and reaping it on exit."""

    @abstractmethod
    async def stop(self) -> None:
        """Stop the runtime, preserving persistent data."""
