"""The logical volume registry and shared storage resolution."""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from pathlib import Path

from arcanus import Relation
from pydantic import UUID7
from uuid_utils.compat import uuid7

from octomate.database import async_session
from octomate.managers.base import Locks, Manager
from octomate.managers.storages import HostStorageManager
from octomate.schemas.volumes import GitVolume, Volume


class VolumeManager(Manager, Locks[UUID7 | None]):
    """Resolve volume records and shared storage handles without creating contents."""

    def __init__(self, *, host_storage: HostStorageManager) -> None:
        self.host_storage: HostStorageManager = host_storage

    async def resolve[VolumeT: Volume](self, volume: VolumeT) -> VolumeT:
        """Reuse this location's runtime coordination across separately loaded rows."""
        volume.storage = self.host_storage.get(volume.storage.path)
        if isinstance(volume, GitVolume):
            mirror = await volume.mirror
            if mirror is not None:
                mirror.storage = self.host_storage.get(mirror.storage.path)
        return volume

    async def mirror(self, project_id: UUID7 | None, path: Path) -> GitVolume:
        """Register a persistent service mirror without creating its files."""
        async with self.lock(project_id), async_session() as session:
            volume = await session.one_or_none(
                GitVolume,
                expressions=[
                    GitVolume["usage"] == "mirror",
                    GitVolume["project_id"].is_(None)
                    if project_id is None
                    else GitVolume["project_id"] == project_id,
                ],
            )
            if volume is None:
                volume = GitVolume(
                    usage="mirror",
                    project_id=project_id,
                    storage=self.host_storage.get(path),
                )
                session.add(volume)
                await session.commit()
            return await self.resolve(volume)

    async def find(
        self, owner_id: UUID7, thread_id: UUID7, project_id: UUID7 | None
    ) -> GitVolume | None:
        """This owner's tree for this thread and project, if one was ever opened."""
        async with async_session() as session:
            volume = await session.one_or_none(
                Volume,
                expressions=[
                    Volume["usage"] == "tree",
                    Volume["owner_id"] == owner_id,
                    Volume["thread_id"] == thread_id,
                    Volume["project_id"].is_(None)
                    if project_id is None
                    else Volume["project_id"] == project_id,
                ],
            )
        if volume is not None and not isinstance(volume, GitVolume):
            raise TypeError("This scope requires a Git working volume")
        return await self.resolve(volume) if volume is not None else None

    async def tree(
        self,
        owner_id: UUID7,
        thread_id: UUID7,
        project_id: UUID7 | None,
        ref: str | None = None,
        *,
        mirror: GitVolume | None = None,
    ) -> GitVolume:
        """This owner's tree for this thread and project, its row written now when
        there is none, and stamped as used either way.

        Registering a volume does not prepare its storage. `ref` is where a first
        fork starts, kept on the row; an existing row keeps its initial ref.
        """
        async with self.lock(thread_id):
            volume = await self.find(owner_id, thread_id, project_id)
            async with async_session() as session:
                if volume is None:
                    volume_id = uuid7()
                    volume = GitVolume(
                        id=volume_id,
                        storage=self.host_storage.get(
                            self.host_storage.volumes_dir
                            / str(owner_id)
                            / "tree"
                            / str(volume_id)
                        ),
                        usage="tree",
                        owner_id=owner_id,
                        thread_id=thread_id,
                        project_id=project_id,
                        base_ref=ref,
                        mirror_id=mirror.id if mirror is not None else None,
                        mirror=Relation(mirror),
                        ephemeral=project_id is None,
                    )
                else:
                    volume.touch()
                    if mirror is not None and volume.mirror_id != mirror.id:
                        volume.mirror_id = mirror.id
                        volume.mirror.value = mirror
                session.add(volume)
                await session.commit()
            return volume

    async def project_trees(self) -> Sequence[GitVolume]:
        """Registered Git working volumes eligible for project retention checks."""
        async with async_session() as session:
            volumes = await session.list(
                GitVolume,
                limit=None,
                expressions=[
                    GitVolume["usage"] == "tree",
                    GitVolume["project_id"].is_not(None),
                ],
            )
        return [await self.resolve(volume) for volume in volumes]

    async def owned(self, ids: list[uuid.UUID]) -> set[uuid.UUID]:
        """Which candidate identities belong to registered logical volumes."""
        async with async_session() as session:
            volumes = await session.list(
                Volume, limit=None, expressions=[Volume["id"].in_(ids)]
            )
        return {volume.id for volume in volumes}
