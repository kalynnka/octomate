"""Shared habitat lifecycle and managed runtime capabilities."""

from abc import ABC, abstractmethod
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from anyio.abc import ByteStream
from pydantic import UUID7
from yarl import URL

from octomate.config.habitat import BaseHabitatConfig
from octomate.schemas.volumes import Volume


class HabitatError(RuntimeError):
    """An actionable failure safe to publish without guest or credential output."""


@dataclass(kw_only=True)
class Habitat(ABC):
    """The execution environment core prepares for an agent invocation."""

    config: BaseHabitatConfig  # This environment's settings, including core access.

    @property
    def core_url(self) -> str:
        """Core's HTTP origin as reached from this runtime."""
        address = URL(str(self.config.core_address)).origin()
        match address.host:
            case "0.0.0.0":
                return str(address.with_host("127.0.0.1"))
            case "::":
                return str(address.with_host("::1"))
        return str(address)

    @property
    @abstractmethod
    def name(self) -> str:
        """Stable identifier of this habitat."""

    @abstractmethod
    def path(self, volume: Volume) -> Path | PurePosixPath:
        """The volume location visible to the harness in this environment."""

    @abstractmethod
    async def prepare(self) -> None:
        """Make the environment ready before an adapter runs in it."""

    @abstractmethod
    async def stop(self) -> None:
        """Release the environment's runtime resources, preserving persistent data."""


class ManagedHabitat(Habitat):
    """One registered user's persistent runtime, hosting agent processes."""

    user_id: UUID7  # The registered user whose private runtime this is.

    def path(self, volume: Volume) -> PurePosixPath:
        raise HabitatError("Managed habitat volume mounts are not yet supported")

    @property
    @abstractmethod
    def idle_timeout(self) -> float:
        """Seconds to keep the runtime after its last invocation ends."""

    @abstractmethod
    def execute(
        self, *command: str, workdir: str, env: dict[str, str] | None = None
    ) -> AbstractAsyncContextManager[ByteStream]:
        """Run a command with duplex stdio, closing and reaping it on exit."""
