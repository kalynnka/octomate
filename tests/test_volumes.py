"""The volume record: each kind round-trips as itself, and the table keeps one
volume per owner where the model says there is one."""

from pathlib import Path

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine

from octomate.database import async_session
from octomate.schemas.project import Project
from octomate.schemas.user import User
from octomate.schemas.volumes import CacheVolume, MemoryVolume, TreeVolume, Volume
from tests.support.managers import a_project, a_thread
from tests.support.users import a_user


@pytest.fixture
async def project(in_memory_engine: AsyncEngine, tmp_path: Path) -> Project:
    project = a_project(tmp_path / "repo")
    async with async_session() as session:
        session.add(project)
        await session.commit()
    return project


async def add(*volumes: Volume) -> None:
    async with async_session() as session:
        session.add_all(volumes)
        await session.commit()


async def test_each_kind_loads_back_as_itself(project: Project) -> None:
    owner = await a_user()
    thread_id = await a_thread()
    tree = TreeVolume(
        owner_id=owner.id,
        thread_id=thread_id,
        project_id=project.id,
        base_ref="refs/heads/main",
    )
    chat_tree = TreeVolume(owner_id=owner.id, thread_id=thread_id)
    memory = MemoryVolume(owner_id=owner.id)
    cache = CacheVolume(owner_id=owner.id)
    await add(tree, chat_tree, memory, cache)

    # A filtered read, not `session.get`: Arcanus answers `get` on the base with
    # the base transmuter, losing the kind.
    async with async_session() as session:
        loaded = [
            await session.one_or_none(Volume, expressions=[Volume["id"] == volume.id])
            for volume in (tree, chat_tree, memory, cache)
        ]

    assert [type(volume) for volume in loaded] == [
        TreeVolume,
        TreeVolume,
        MemoryVolume,
        CacheVolume,
    ]
    assert isinstance(loaded[0], TreeVolume)
    assert (loaded[0].project_id, loaded[0].base_ref) == (
        project.id,
        "refs/heads/main",
    )
    assert isinstance(loaded[1], TreeVolume)
    assert loaded[1].project_id is None


@pytest.mark.parametrize("kind", [MemoryVolume, CacheVolume])
async def test_one_memory_and_one_cache_per_owner(
    in_memory_engine: AsyncEngine, kind: type[MemoryVolume | CacheVolume]
) -> None:
    alice, bob = await a_user("alice"), await a_user("bob")
    await add(kind(owner_id=alice.id), kind(owner_id=bob.id))

    with pytest.raises(IntegrityError):
        await add(kind(owner_id=alice.id))


async def test_one_tree_per_owner_thread_and_project(project: Project) -> None:
    alice, bob = await a_user("alice"), await a_user("bob")
    thread_id = await a_thread()
    other = a_project(project.root.with_name("other"))
    async with async_session() as session:
        session.add(other)
        await session.commit()
    # Each owner gets their own trees in a shared thread, one per project, plus
    # one tree in no project.
    await add(
        *(
            TreeVolume(owner_id=owner.id, thread_id=thread_id, project_id=project_id)
            for owner in (alice, bob)
            for project_id in (project.id, other.id, None)
        )
    )

    with pytest.raises(IntegrityError):
        await add(
            TreeVolume(owner_id=alice.id, thread_id=thread_id, project_id=project.id)
        )
    with pytest.raises(IntegrityError):
        await add(TreeVolume(owner_id=alice.id, thread_id=thread_id))


async def test_an_owner_with_volumes_cannot_be_deleted(
    in_memory_engine: AsyncEngine,
) -> None:
    """The row goes after its folder, and the owner after the row; a cascade
    would drop the row while the folder is still on disk."""
    owner: User = await a_user()
    await add(MemoryVolume(owner_id=owner.id))

    async with async_session() as session:
        loaded = await session.get(User, owner.id)
        assert loaded is not None
        await session.delete(loaded)
        with pytest.raises(IntegrityError):
            await session.commit()
