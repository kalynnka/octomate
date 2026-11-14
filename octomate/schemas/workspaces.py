"""A run's volume composition, independent of persisted volume identities."""

from __future__ import annotations

from collections.abc import AsyncGenerator
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import NotRequired, TypedDict

from pydantic import UUID7

from octomate.config.mirrors import GitIdentity
from octomate.schemas.project import Project
from octomate.schemas.volumes import GitVolume, Volume


class WorkspaceVolumes(TypedDict):
    """A run's volumes by the part each plays; mirrors stay outside."""

    work: Volume  # The harness's working directory.
    cache: NotRequired[Volume]  # The owner's package cache; registered owners only.
    # The owner's agent configuration, which a sandbox's agents keep theirs in.
    config: NotRequired[Volume]


@dataclass(frozen=True, kw_only=True)
class Workspace:
    """Volumes used together for a run; entry owns their combined lifetime.

    Volumes supply their own storage. This context prepares storage only;
    making those paths accessible belongs to the habitat provider.
    """

    thread_id: UUID7
    volumes: WorkspaceVolumes
    identity: GitIdentity | None = None  # Identity used when checkpointing Git volumes.
    project: Project | None = None

    def __post_init__(self) -> None:
        for volume in self.entries:
            if volume.usage == "mirror":
                raise ValueError(
                    "Host mirrors cannot be included in an agent workspace"
                )
            if volume.owner_id != self.owner_id:
                raise ValueError("Workspace volume owners do not match")

    @property
    def volume(self) -> Volume:
        return self.volumes["work"]

    @property
    def entries(self) -> list[Volume]:
        """The volumes present, in the order a run enters them."""
        kept = (self.volumes.get("cache"), self.volumes.get("config"))
        return [self.volume, *(volume for volume in kept if volume is not None)]

    @property
    def owner_id(self) -> UUID7 | None:
        return self.volume.owner_id

    @property
    def path(self) -> Path:
        """The working directory on the host."""
        return self.volume.path

    @asynccontextmanager
    async def __call__(self) -> AsyncGenerator[Workspace]:
        """Prepare all entries and unwind them in reverse order, including on failure."""
        async with AsyncExitStack() as resources:
            for volume in self.entries:
                if isinstance(volume, GitVolume):
                    await resources.enter_async_context(volume(identity=self.identity))
                else:
                    await resources.enter_async_context(volume())
            yield self
