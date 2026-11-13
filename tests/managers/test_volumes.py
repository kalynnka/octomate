"""Volume registration is shared, persistent, and independent of host directories."""

import asyncio
from datetime import UTC, datetime
from pathlib import Path

import anyio
from sqlalchemy.ext.asyncio import AsyncEngine
from uuid_utils.compat import uuid7

from octomate import Octomate
from octomate.database import async_session
from octomate.managers.volumes import VolumeManager
from octomate.managers.workspaces import WorkspaceManager
from octomate.schemas.volumes import GitVolume, Volume
from tests.support.managers import a_project, a_registry, a_thread
from tests.support.users import a_user


async def test_missing_lookup_creates_neither_rows_nor_directories(
    in_memory_engine: AsyncEngine, tmp_path: Path
) -> None:
    owner = await a_user()
    thread_id = await a_thread()
    volumes = VolumeManager()
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
    volumes = VolumeManager()
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
    volumes = VolumeManager()
    first = WorkspaceManager(volumes=volumes, volumes_dir=tmp_path / "volumes")
    second = WorkspaceManager(volumes=volumes, volumes_dir=tmp_path / "volumes")

    resolved = await asyncio.gather(
        first.resolve(thread_id, None, owner.id),
        second.resolve(thread_id, None, owner.id),
    )

    assert resolved[0].volume.id == resolved[1].volume.id
    assert resolved[0].path == resolved[1].path
    assert not resolved[0].path.exists()
    async with async_session() as session:
        assert await session.count(Volume) == 1
    assert Octomate(workspaces=first).volumes is volumes


async def test_scopes_and_retention_queries_preserve_volume_identity(
    in_memory_engine: AsyncEngine, tmp_path: Path
) -> None:
    alice, bob = await a_user("alice"), await a_user("bob")
    thread_id = await a_thread()
    project = a_project(tmp_path / "project")
    await a_registry(project)
    volumes = VolumeManager()
    paths_before = {path async for path in anyio.Path(tmp_path).iterdir()}

    project_tree = await volumes.tree(alice.id, thread_id, project.id)
    chat_tree = await volumes.tree(alice.id, thread_id, None)
    peer_tree = await volumes.tree(bob.id, thread_id, None)
    directory = Volume(owner_id=alice.id, usage="cache")
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
