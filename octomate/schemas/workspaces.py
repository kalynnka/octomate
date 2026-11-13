"""A run's volume composition, independent of persisted volume identities."""

from __future__ import annotations

from collections.abc import AsyncGenerator
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

import anyio
from pydantic import UUID7

from octomate.config.mirrors import GitIdentity
from octomate.habitat.base import Attachment
from octomate.schemas.project import Project
from octomate.schemas.volumes import GitVolume, Volume


@dataclass(frozen=True, kw_only=True)
class Workspace:
    """Volumes used together for a run; entry owns their combined lifetime.

    Attachments describe habitat paths. This context prepares storage only;
    making those paths accessible belongs to the habitat provider.
    """

    thread_id: UUID7
    volumes: dict[str, Volume | None]  # In entry order; None is an unregistered chat.
    attachments: dict[str, Attachment]  # Habitat mappings keyed by volume name.
    mirrors: dict[str, Path] = field(default_factory=dict)  # Sources for Git volumes.
    identity: GitIdentity | None = None  # Identity used when checkpointing Git volumes.
    directory: str = "work"  # Entry whose attachment is the harness working directory.
    project: Project | None = None

    def __post_init__(self) -> None:
        if self.directory not in self.volumes:
            raise ValueError("The workspace working directory must name a volume entry")
        if self.volumes.keys() != self.attachments.keys():
            raise ValueError(
                "Workspace volumes and attachments must have the same names"
            )
        for volume in self.volumes.values():
            owner_id = volume.owner_id if volume is not None else None
            if owner_id != self.owner_id:
                raise ValueError("Workspace volume owners do not match")

    @property
    def attachment(self) -> Attachment:
        return self.attachments[self.directory]

    @property
    def volume(self) -> Volume | None:
        return self.volumes[self.directory]

    @property
    def owner_id(self) -> UUID7 | None:
        return self.volume.owner_id if self.volume is not None else None

    @property
    def path(self) -> Path:
        """The working directory on the host."""
        return self.attachment.storage.path

    @property
    def cwd(self) -> Path | PurePosixPath:
        """The working directory visible to the harness in its habitat."""
        return self.attachment.destination

    @asynccontextmanager
    async def __call__(self) -> AsyncGenerator[Workspace]:
        """Prepare all entries and unwind them in reverse order, including on failure."""
        async with AsyncExitStack() as resources:
            for name, volume in self.volumes.items():
                storage = self.attachments[name].storage
                if volume is None:
                    path = await resources.enter_async_context(
                        storage.acquire(ephemeral=True)
                    )
                    await anyio.Path(path).mkdir(parents=True, exist_ok=True)
                elif isinstance(volume, GitVolume):
                    await resources.enter_async_context(
                        volume(
                            mirror=self.mirrors.get(name),
                            identity=self.identity,
                        )
                    )
                else:
                    await resources.enter_async_context(volume())
            yield self
