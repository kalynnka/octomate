"""A run's volume composition, independent of persisted volume identities."""

from __future__ import annotations

from collections.abc import AsyncGenerator
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass
from pathlib import Path

from pydantic import UUID7

from octomate.config.mirrors import GitIdentity
from octomate.schemas.project import Project
from octomate.schemas.volumes import GitVolume, Volume


@dataclass(frozen=True, kw_only=True)
class Workspace:
    """Volumes used together for a run; entry owns their combined lifetime.

    Volumes supply their own storage. This context prepares storage only;
    making those paths accessible belongs to the habitat provider.
    """

    thread_id: UUID7
    volumes: dict[str, Volume]  # Working volumes in entry order; mirrors stay outside.
    identity: GitIdentity | None = None  # Identity used when checkpointing Git volumes.
    directory: str = "work"  # Entry selected as the harness working directory.
    project: Project | None = None

    def __post_init__(self) -> None:
        if self.directory not in self.volumes:
            raise ValueError("The workspace working directory must name a volume entry")
        for volume in self.volumes.values():
            if volume.usage == "mirror":
                raise ValueError(
                    "Host mirrors cannot be included in an agent workspace"
                )
            if volume.owner_id != self.owner_id:
                raise ValueError("Workspace volume owners do not match")

    @property
    def volume(self) -> Volume:
        return self.volumes[self.directory]

    @property
    def owner_id(self) -> UUID7 | None:
        return self.volume.owner_id

    @property
    def path(self) -> Path:
        """The working directory on the host."""
        return self.volume.storage.path

    @asynccontextmanager
    async def __call__(self) -> AsyncGenerator[Workspace]:
        """Prepare all entries and unwind them in reverse order, including on failure."""
        async with AsyncExitStack() as resources:
            for volume in self.volumes.values():
                if isinstance(volume, GitVolume):
                    await resources.enter_async_context(volume(identity=self.identity))
                else:
                    await resources.enter_async_context(volume())
            yield self
