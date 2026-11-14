"""The volume record: each kind round-trips as itself, and the table keeps one
volume per owner where the model says there is one."""

from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import ValidationError
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine
from uuid_utils.compat import uuid7

from octomate.database import async_session
from octomate.schemas.project import Project
from octomate.schemas.user import User
from octomate.schemas.volumes import GitVolume, Volume
from octomate.schemas.volumes.storage import HostDirectory
from octomate.types.volumes import VolumeUsage
from tests.support.managers import a_project, a_thread, a_workspace_manager
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


async def test_user_root_and_volume_paths_do_not_create_rows_or_directories(
    project: Project, tmp_path: Path
) -> None:
    alice, bob = await a_user("alice"), await a_user("bob")
    manager = a_workspace_manager(volumes_dir=tmp_path / "volumes")
    root = manager.root(alice.id)
    assert root == tmp_path / "volumes" / str(alice.id)
    assert root != manager.root(bob.id)
    volumes = (
        GitVolume(
            storage=HostDirectory(Path("storage") / str(uuid7())),
            usage="tree",
            owner_id=alice.id,
            thread_id=await a_thread(),
            project_id=project.id,
        ),
        Volume(
            storage=HostDirectory(Path("storage") / str(uuid7())),
            usage="memory",
            owner_id=alice.id,
        ),
        Volume(
            storage=HostDirectory(Path("storage") / str(uuid7())),
            usage="cache",
            owner_id=alice.id,
        ),
    )
    for volume in volumes:
        assert manager.path(volume) == volume.storage.path
        assert not manager.path(volume).is_relative_to(manager.root(bob.id))
    async with async_session() as session:
        assert await session.count(Volume) == 0
    assert not manager.volumes_dir.exists()


async def test_each_kind_loads_back_as_itself(project: Project) -> None:
    owner = await a_user()
    thread_id = await a_thread()
    tree = GitVolume(
        storage=HostDirectory(Path("storage") / str(uuid7())),
        usage="tree",
        owner_id=owner.id,
        thread_id=thread_id,
        project_id=project.id,
        base_ref="refs/heads/main",
    )
    chat_tree = GitVolume(
        storage=HostDirectory(Path("storage") / str(uuid7())),
        usage="tree",
        owner_id=owner.id,
        thread_id=thread_id,
    )
    memory = Volume(
        storage=HostDirectory(Path("storage") / str(uuid7())),
        usage="memory",
        owner_id=owner.id,
    )
    cache = Volume(
        storage=HostDirectory(Path("storage") / str(uuid7())),
        usage="cache",
        owner_id=owner.id,
    )
    await add(tree, chat_tree, memory, cache)

    # A filtered read, not `session.get`: Arcanus answers `get` on the base with
    # the base transmuter, losing the kind.
    async with async_session() as session:
        loaded = [
            await session.one_or_none(Volume, expressions=[Volume["id"] == volume.id])
            for volume in (tree, chat_tree, memory, cache)
        ]

    assert [type(volume) for volume in loaded] == [
        GitVolume,
        GitVolume,
        Volume,
        Volume,
    ]
    assert isinstance(loaded[0], GitVolume)
    assert (loaded[0].project_id, loaded[0].base_ref) == (
        project.id,
        "refs/heads/main",
    )
    assert isinstance(loaded[1], GitVolume)
    assert loaded[1].project_id is None


@pytest.mark.parametrize("usage", ["memory", "cache"])
async def test_one_memory_and_one_cache_per_owner(
    in_memory_engine: AsyncEngine, usage: VolumeUsage
) -> None:
    alice, bob = await a_user("alice"), await a_user("bob")
    await add(
        Volume(
            storage=HostDirectory(Path("storage") / str(uuid7())),
            usage=usage,
            owner_id=alice.id,
        ),
        Volume(
            storage=HostDirectory(Path("storage") / str(uuid7())),
            usage=usage,
            owner_id=bob.id,
        ),
    )

    with pytest.raises(IntegrityError):
        await add(
            Volume(
                storage=HostDirectory(Path("storage") / str(uuid7())),
                usage=usage,
                owner_id=alice.id,
            )
        )


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
            GitVolume(
                storage=HostDirectory(Path("storage") / str(uuid7())),
                usage="tree",
                owner_id=owner.id,
                thread_id=thread_id,
                project_id=project_id,
            )
            for owner in (alice, bob)
            for project_id in (project.id, other.id, None)
        )
    )

    with pytest.raises(IntegrityError):
        await add(
            GitVolume(
                storage=HostDirectory(Path("storage") / str(uuid7())),
                usage="tree",
                owner_id=alice.id,
                thread_id=thread_id,
                project_id=project.id,
            )
        )
    with pytest.raises(IntegrityError):
        await add(
            GitVolume(
                storage=HostDirectory(Path("storage") / str(uuid7())),
                usage="tree",
                owner_id=alice.id,
                thread_id=thread_id,
            )
        )


async def test_an_owner_with_volumes_cannot_be_deleted(
    in_memory_engine: AsyncEngine,
) -> None:
    """The row goes after its folder, and the owner after the row; a cascade
    would drop the row while the folder is still on disk."""
    owner: User = await a_user()
    await add(
        Volume(
            storage=HostDirectory(Path("storage") / str(uuid7())),
            usage="memory",
            owner_id=owner.id,
        )
    )

    async with async_session() as session:
        loaded = await session.get(User, owner.id)
        assert loaded is not None
        await session.delete(loaded)
        with pytest.raises(IntegrityError):
            await session.commit()


async def test_unscoped_volumes_round_trip_without_a_directory(
    in_memory_engine: AsyncEngine, tmp_path: Path
) -> None:
    owner = await a_user()
    volumes = [
        Volume(
            storage=HostDirectory(Path("storage") / str(uuid7())), owner_id=owner.id
        ),
        GitVolume(
            storage=HostDirectory(Path("storage") / str(uuid7())),
            owner_id=owner.id,
            base_ref="refs/tags/start",
        ),
        GitVolume(
            storage=HostDirectory(Path("storage") / str(uuid7())), owner_id=owner.id
        ),
    ]
    await add(*volumes)

    async with async_session() as session:
        loaded = await session.list(Volume, limit=None)

    assert {volume.id for volume in loaded} == {volume.id for volume in volumes}
    assert sum(isinstance(volume, GitVolume) for volume in loaded) == 2
    assert all(volume.thread_id is None and volume.usage is None for volume in loaded)
    assert not (tmp_path / ".octomate/volumes").exists()


async def test_usage_uniqueness_applies_across_content_kinds(
    in_memory_engine: AsyncEngine,
) -> None:
    owner = await a_user()
    await add(
        GitVolume(
            storage=HostDirectory(Path("storage") / str(uuid7())),
            owner_id=owner.id,
            usage="memory",
        )
    )
    with pytest.raises(IntegrityError):
        await add(
            Volume(
                storage=HostDirectory(Path("storage") / str(uuid7())),
                owner_id=owner.id,
                usage="memory",
            )
        )


async def test_workspace_runtime_refuses_an_existing_plain_tree(
    in_memory_engine: AsyncEngine,
) -> None:
    owner = await a_user()
    thread_id = await a_thread()
    volume = Volume(
        storage=HostDirectory(Path("storage") / str(uuid7())),
        owner_id=owner.id,
        usage="tree",
        thread_id=thread_id,
    )
    await add(volume)
    manager = a_workspace_manager()
    with pytest.raises(TypeError, match="requires a Git working volume"):
        await manager.volumes.tree(owner.id, thread_id, None)
    manager.path(volume).mkdir(parents=True)
    assert await manager.report_unowned() == 0


@pytest.mark.parametrize("field", ["thread_id", "project_id"])
def test_only_working_tree_usage_accepts_associations(field: str) -> None:
    with pytest.raises(ValidationError, match="Only working trees and mirrors"):
        Volume.model_validate(
            {
                "owner_id": uuid7(),
                "storage": HostDirectory(Path("storage")),
                field: uuid7(),
            }
        )


def test_working_tree_usage_requires_a_thread() -> None:
    with pytest.raises(ValidationError, match="requires a thread"):
        GitVolume(
            storage=HostDirectory(Path("storage") / str(uuid7())),
            owner_id=uuid7(),
            usage="tree",
        )


async def test_database_rejects_associations_without_usage(
    in_memory_engine: AsyncEngine,
) -> None:
    owner = await a_user()
    volume = GitVolume(
        storage=HostDirectory(Path("storage") / str(uuid7())),
        owner_id=owner.id,
        usage="tree",
        thread_id=await a_thread(),
    )
    await add(volume)
    with pytest.raises(IntegrityError):
        async with in_memory_engine.begin() as connection:
            await connection.execute(
                text("UPDATE volumes SET usage = NULL WHERE id = :id"),
                {"id": volume.id.hex},
            )


async def test_loaded_volume_owns_activity_and_git_reference_policy(
    project: Project,
) -> None:
    owner = await a_user()
    before = datetime(2025, 1, 1, tzinfo=UTC)
    tree = GitVolume(
        storage=HostDirectory(Path("storage") / str(uuid7())),
        owner_id=owner.id,
        usage="tree",
        thread_id=await a_thread(),
        project_id=project.id,
        last_used_at=before,
    )
    independent = GitVolume(
        storage=HostDirectory(Path("storage") / str(uuid7())),
        owner_id=owner.id,
        last_used_at=before,
    )
    await add(tree, independent)

    async with async_session() as session:
        loaded = await session.list(Volume, limit=None)
        for volume in loaded:
            assert isinstance(volume, GitVolume)
            assert volume.snapshot_ref == f"refs/octomate/trees/{volume.id}"
            expected = (
                f"octomate/thread-{tree.thread_id}"
                if volume.id == tree.id
                else f"octomate/volume-{independent.id}"
            )
            assert volume.branch == expected
            volume.touch()
        await session.commit()

    async with async_session() as session:
        reloaded = await session.list(Volume, limit=None)
    assert all(volume.last_used_at > before for volume in reloaded)
    assert {volume.id for volume in reloaded} == {tree.id, independent.id}


async def test_unregistered_temporary_volume_cannot_be_persisted(
    in_memory_engine: AsyncEngine,
    tmp_path: Path,
) -> None:
    volume = Volume(storage=HostDirectory(tmp_path / "temporary"), ephemeral=True)
    async with volume() as path:
        assert path.is_dir()
    assert not path.exists()
    with pytest.raises(IntegrityError, match="ck_volumes_persisted_owner"):
        await add(volume)


@pytest.mark.parametrize("project_mirror", [False, True])
async def test_only_one_service_mirror_per_project_or_blank(
    project: Project,
    tmp_path: Path,
    project_mirror: bool,
) -> None:
    project_id = project.id if project_mirror else None
    first = GitVolume(
        usage="mirror", project_id=project_id, storage=HostDirectory(tmp_path / "first")
    )
    duplicate = GitVolume(
        usage="mirror",
        project_id=project_id,
        storage=HostDirectory(tmp_path / "second"),
    )
    await add(first)
    with pytest.raises(IntegrityError):
        await add(duplicate)


def test_mirror_cannot_be_a_user_owned_or_ephemeral_volume(tmp_path: Path) -> None:
    with pytest.raises(ValidationError, match="service-owned"):
        GitVolume(
            usage="mirror", owner_id=uuid7(), storage=HostDirectory(tmp_path / "owned")
        )
    with pytest.raises(ValidationError, match="persistent"):
        GitVolume(
            usage="mirror",
            ephemeral=True,
            storage=HostDirectory(tmp_path / "ephemeral"),
        )
