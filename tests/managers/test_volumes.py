"""Volume registration persists locations without creating their contents."""

import asyncio
from datetime import UTC, datetime
from pathlib import Path

import anyio
from pydantic import TypeAdapter
from sqlalchemy.ext.asyncio import AsyncEngine
from uuid_utils.compat import uuid7

from octomate.database import async_session
from octomate.managers.storages import HostStorageManager
from octomate.managers.volumes import VolumeManager
from octomate.managers.workspaces.mirrors import MirrorManager
from octomate.schemas.volumes import GitVolume, Volume
from octomate.schemas.volumes.storage import HostDirectory
from tests.support.managers import a_project, a_registry, a_thread, a_workspace_manager
from tests.support.users import a_user


async def test_missing_lookup_creates_neither_rows_nor_directories(
    in_memory_engine: AsyncEngine, tmp_path: Path
) -> None:
    owner = await a_user()
    thread_id = await a_thread()
    volumes = VolumeManager(host_storage=HostStorageManager())
    paths_before = {path async for path in anyio.Path(tmp_path).iterdir()}

    assert await volumes.find(owner.id, thread_id, None) is None

    async with async_session() as session:
        assert await session.count(Volume) == 0
    assert {path async for path in anyio.Path(tmp_path).iterdir()} == paths_before


async def test_lookup_does_not_touch_activity_but_acquisition_does(
    in_memory_engine: AsyncEngine, tmp_path: Path
) -> None:
    owner = await a_user()
    before = datetime(2025, 1, 1, tzinfo=UTC)
    original = GitVolume(
        storage=HostDirectory(Path("storage") / str(uuid7())),
        owner_id=owner.id,
        usage="tree",
        thread_id=await a_thread(),
        base_ref="refs/tags/start",
        ephemeral=True,
        last_used_at=before,
    )
    async with async_session() as session:
        session.add(original)
        await session.commit()
    volumes = VolumeManager(host_storage=HostStorageManager())
    paths_before = {path async for path in anyio.Path(tmp_path).iterdir()}
    assert original.thread_id is not None

    found = await volumes.find(owner.id, original.thread_id, None)
    assert found is not None
    assert found.last_used_at == before
    reused = await volumes.tree(owner.id, original.thread_id, None, "other")

    assert reused.id == original.id
    assert reused.base_ref == original.base_ref
    assert reused.created_at == original.created_at
    reloaded = await volumes.find(owner.id, original.thread_id, None)
    assert reloaded is not None
    assert reloaded.last_used_at > before
    assert {path async for path in anyio.Path(tmp_path).iterdir()} == paths_before


async def test_shared_registry_serializes_allocation_across_workspace_managers(
    in_memory_engine: AsyncEngine, tmp_path: Path
) -> None:
    owner = await a_user()
    thread_id = await a_thread()
    volumes = VolumeManager(host_storage=HostStorageManager(root=tmp_path))
    first = a_workspace_manager(volumes=volumes)
    second = a_workspace_manager(volumes=volumes)

    resolved = await asyncio.gather(
        first.resolve(thread_id, None, owner.id),
        second.resolve(thread_id, None, owner.id),
    )

    assert resolved[0].volume is not None
    assert resolved[1].volume is not None
    assert resolved[0].volume.id == resolved[1].volume.id
    assert resolved[0].path == resolved[1].path
    assert not resolved[0].path.exists()
    first_cache = resolved[0].volumes.get("cache")
    second_cache = resolved[1].volumes.get("cache")
    assert first_cache is not None
    assert second_cache is not None
    assert first_cache.id == second_cache.id
    # The tree, the owner's cache and the blank mirror the tree forks from.
    async with async_session() as session:
        assert await session.count(Volume) == 3
    assert first.volumes is second.volumes is volumes


async def test_reloaded_volumes_share_active_storage_lifetime(
    in_memory_engine: AsyncEngine,
) -> None:
    owner = await a_user()
    thread_id = await a_thread()
    volumes = VolumeManager(host_storage=HostStorageManager())
    mirror = await MirrorManager(volumes=volumes).create()
    first = await volumes.tree(owner.id, thread_id, None, mirror=mirror)

    async with first() as path:
        (path / "work").write_text("retained")
        second = await volumes.find(owner.id, thread_id, None)
        assert second is not None
        assert second.storage is first.storage
        assert second.storage.uses == 1
        async with second():
            assert first.storage.uses == 2
            assert (path / "work").read_text() == "retained"
        assert first.storage.uses == 1
    assert first.storage.uses == 0
    assert not path.exists()


async def test_recorded_storage_moves_with_the_root(
    in_memory_engine: AsyncEngine, tmp_path: Path
) -> None:
    owner = await a_user()
    volumes = VolumeManager(host_storage=HostStorageManager(root=tmp_path / "first"))
    original = await volumes.tree(owner.id, await a_thread(), None)
    assert original.thread_id is not None
    beneath = original.path.relative_to(tmp_path / "first")
    recorded = TypeAdapter(HostDirectory).dump_python(original.storage, mode="json")
    assert recorded == {"kind": "host-directory", "path": str(beneath)}

    moved = VolumeManager(host_storage=HostStorageManager(root=tmp_path / "second"))
    reloaded = await moved.find(owner.id, original.thread_id, None)

    assert reloaded is not None
    assert reloaded.path == tmp_path / "second" / beneath
    assert not reloaded.path.exists()


async def test_an_owner_keeps_one_cache_apart_from_other_owners(
    in_memory_engine: AsyncEngine, tmp_path: Path
) -> None:
    alice, bob = await a_user("alice"), await a_user("bob")
    volumes = VolumeManager(host_storage=HostStorageManager(root=tmp_path))

    first, again = await asyncio.gather(
        volumes.cache(alice.id), volumes.cache(alice.id)
    )
    other = await volumes.cache(bob.id)
    reloaded = await VolumeManager(
        host_storage=HostStorageManager(root=tmp_path)
    ).cache(alice.id)

    assert first.id == again.id == reloaded.id != other.id
    assert first.storage is again.storage
    assert first.path == (
        tmp_path / "volumes" / str(alice.id) / "cache" / str(first.id)
    )
    assert reloaded.path == first.path
    assert other.path.is_relative_to(tmp_path / "volumes" / str(bob.id))
    assert not first.ephemeral
    assert not first.path.exists()
    async with async_session() as session:
        assert await session.count(Volume) == 2


async def test_scopes_and_retention_queries_preserve_volume_identity(
    in_memory_engine: AsyncEngine, tmp_path: Path
) -> None:
    alice, bob = await a_user("alice"), await a_user("bob")
    thread_id = await a_thread()
    project = a_project(tmp_path / "project")
    await a_registry(project)
    volumes = VolumeManager(host_storage=HostStorageManager())
    paths_before = {path async for path in anyio.Path(tmp_path).iterdir()}

    project_tree = await volumes.tree(alice.id, thread_id, project.id)
    chat_tree = await volumes.tree(alice.id, thread_id, None)
    peer_tree = await volumes.tree(bob.id, thread_id, None)
    directory = Volume(
        storage=HostDirectory(Path("storage") / str(uuid7())),
        owner_id=alice.id,
        usage="cache",
    )
    async with async_session() as session:
        session.add(directory)
        await session.commit()

    assert len({project_tree.id, chat_tree.id, peer_tree.id}) == 3
    assert not project_tree.ephemeral
    assert chat_tree.ephemeral
    assert peer_tree.ephemeral
    assert [volume.id for volume in await volumes.project_trees()] == [project_tree.id]
    assert await volumes.owned([project_tree.id, directory.id, uuid7()]) == {
        project_tree.id,
        directory.id,
    }
    assert {path async for path in anyio.Path(tmp_path).iterdir()} == paths_before


async def test_mirror_identity_and_location_survive_registry_recreation(
    in_memory_engine: AsyncEngine,
    tmp_path: Path,
) -> None:
    first = VolumeManager(host_storage=HostStorageManager())
    mirror, concurrent = await asyncio.gather(
        first.mirror(None, tmp_path / "original"),
        first.mirror(None, tmp_path / "original"),
    )
    reloaded = await VolumeManager(host_storage=HostStorageManager()).mirror(
        None, tmp_path / "changed"
    )
    assert mirror.id == concurrent.id == reloaded.id
    assert reloaded.owner_id is None
    assert reloaded.usage == "mirror"
    assert not reloaded.ephemeral
    assert reloaded.storage.path == tmp_path / "original"
    assert not reloaded.path.exists()
    async with async_session() as session:
        assert await session.count(Volume) == 1


async def test_tree_reloads_its_service_mirror_without_an_attachment(
    in_memory_engine: AsyncEngine,
    tmp_path: Path,
) -> None:
    owner = await a_user()
    volumes = VolumeManager(host_storage=HostStorageManager())
    mirror = await volumes.mirror(None, tmp_path / "mirror")
    tree = await volumes.tree(owner.id, await a_thread(), None, mirror=mirror)
    assert tree.thread_id is not None
    loaded = await VolumeManager(host_storage=HostStorageManager()).find(
        owner.id, tree.thread_id, None
    )
    assert loaded is not None
    source = await loaded.mirror
    assert source is not None
    assert loaded.mirror_id == source.id == mirror.id
    assert source.storage.path == mirror.storage.path
    assert source.owner_id is None
    assert not source.storage.path.exists()
    assert not loaded.storage.path.exists()
