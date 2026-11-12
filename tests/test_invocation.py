import asyncio
from pathlib import Path, PurePosixPath
from unittest.mock import AsyncMock

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine
from uuid_utils.compat import uuid7

from octomate import Octomate
from octomate.config.habitat import SbxHabitatConfig
from octomate.habitat import Habitat, HabitatError, NativeHabitat
from octomate.habitat.sbx import SbxHabitat
from octomate.managers.habitat import Invocation
from octomate.managers.workspaces.base import (
    TreeWorkspace,
    UnregisteredProjectError,
    WorkspaceManager,
)
from octomate.schemas.thread import ThreadKey
from tests.support.managers import a_project, a_registry, a_thread
from tests.support.users import a_user


@pytest.mark.parametrize("registered", [False, True])
async def test_native_invocation_borrows_the_owners_chat_workspace(
    in_memory_engine: AsyncEngine, registered: bool
) -> None:
    owner_id = (await a_user()).id if registered else None
    thread_id = await a_thread()
    workspaces = WorkspaceManager()
    habitat: Habitat = NativeHabitat()
    await habitat.prepare()
    async with workspaces.open(thread_id, None, owner_id) as workspace:
        invocation = Invocation(
            id=uuid7(),
            conversation_id=uuid7(),
            workspace=workspace,
            habitat=habitat,
        )
        assert invocation.owner_id == owner_id
        assert invocation.thread_id == thread_id
        assert invocation.workspace is workspace
        assert invocation.workspace.path.is_dir()
        assert invocation.habitat is habitat
        assert invocation.cwd == workspace.path
        assert isinstance(invocation.cwd, Path)
        if isinstance(workspace, TreeWorkspace):
            assert workspace.root == workspaces.root(workspace.owner_id)

    await habitat.stop()
    assert not invocation.workspace.path.exists()


async def test_native_invocation_keeps_the_project_workspace_owner(
    in_memory_engine: AsyncEngine, tmp_path: Path
) -> None:
    owner = await a_user()
    root = tmp_path / "project"
    root.mkdir()
    project = a_project(root)
    workspaces = WorkspaceManager(projects=await a_registry(project))
    async with workspaces.open(await a_thread(), project, owner.id) as workspace:
        invocation = Invocation(
            id=uuid7(),
            conversation_id=uuid7(),
            workspace=workspace,
            habitat=NativeHabitat(),
        )
        assert invocation.owner_id == workspace.volume.owner_id == owner.id
        assert invocation.thread_id == workspace.volume.thread_id
        assert invocation.cwd == workspace.path
        assert workspace.root == workspaces.root(owner.id)


async def test_invocation_borrows_the_habitat_without_managing_its_lifetime(
    in_memory_engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    owner = await a_user()
    habitat = SbxHabitat(
        config=SbxHabitatConfig(image="sandbox@sha256:" + "a" * 64),
        user_id=owner.id,
    )
    prepare = AsyncMock()
    stop = AsyncMock()
    monkeypatch.setattr(habitat, "prepare", prepare)
    monkeypatch.setattr(habitat, "stop", stop)
    workspaces = WorkspaceManager()
    async with workspaces.open(await a_thread(), None, owner.id) as workspace:
        invocation = Invocation(
            id=uuid7(),
            conversation_id=uuid7(),
            workspace=workspace,
            habitat=habitat,
        )
        assert invocation.habitat is habitat
        assert invocation.cwd == PurePosixPath("/workspace/tree") / str(
            workspace.volume.id
        )
        assert not isinstance(invocation.cwd, Path)
        assert workspace.path.is_relative_to(workspace.root)

    prepare.assert_not_called()
    stop.assert_not_called()


async def test_project_guest_cwd_is_known_before_materialization(
    in_memory_engine: AsyncEngine, tmp_path: Path
) -> None:
    owner = await a_user()
    project = a_project(tmp_path / "project")
    workspaces = WorkspaceManager(
        projects=await a_registry(project), volumes_dir=tmp_path / "volumes"
    )
    workspace = await workspaces.resolve(await a_thread(), project, owner.id)
    invocation = Invocation(
        id=uuid7(),
        conversation_id=uuid7(),
        workspace=workspace,
        habitat=SbxHabitat(
            config=SbxHabitatConfig(image="sandbox@sha256:" + "a" * 64),
            user_id=owner.id,
        ),
    )
    assert workspace.root == workspaces.root(owner.id)
    assert invocation.cwd == PurePosixPath("/workspace/tree") / str(workspace.volume.id)
    assert not workspace.root.exists()


@pytest.mark.parametrize("registered", [False, True])
async def test_invocation_rejects_an_unregistered_or_different_habitat_owner(
    in_memory_engine: AsyncEngine, registered: bool
) -> None:
    owner_id = (await a_user()).id if registered else None
    habitat = SbxHabitat(
        config=SbxHabitatConfig(image="sandbox@sha256:" + "a" * 64),
        user_id=uuid7(),
    )
    workspace = await WorkspaceManager().resolve(await a_thread(), None, owner_id)
    message = "owners do not match" if registered else "registered user"
    with pytest.raises(ValueError, match=message):
        Invocation(
            id=uuid7(),
            conversation_id=uuid7(),
            workspace=workspace,
            habitat=habitat,
        )


@pytest.mark.parametrize("registered", [False, True])
async def test_core_prepares_and_releases_native_chat(
    in_memory_engine: AsyncEngine, registered: bool
) -> None:
    app = Octomate()
    owner_id = (await a_user()).id if registered else None
    thread = await app.threads.ensure(ThreadKey("im", "thread", "room", "turn"))
    conversation_id = uuid7()
    async with app.habitats.invoke(
        app.workspaces,
        thread=thread,
        conversation_id=conversation_id,
        owner_id=owner_id,
    ) as invocation:
        assert invocation.owner_id == owner_id
        assert invocation.thread_id == thread.id
        assert invocation.conversation_id == conversation_id
        assert isinstance(invocation.habitat, NativeHabitat)
        assert invocation.cwd == invocation.workspace.path
        assert invocation.workspace.path.is_dir()
    assert not invocation.workspace.path.exists()


async def test_core_isolates_two_owners_in_a_shared_thread(
    in_memory_engine: AsyncEngine,
) -> None:
    app = Octomate()
    alice, bob = await a_user("alice"), await a_user("bob")
    thread = await app.threads.ensure(ThreadKey("im", "thread", "room", "turn"))
    conversation_id = uuid7()
    async with (
        app.habitats.invoke(
            app.workspaces,
            thread=thread,
            conversation_id=conversation_id,
            owner_id=alice.id,
        ) as first,
        app.habitats.invoke(
            app.workspaces,
            thread=thread,
            conversation_id=conversation_id,
            owner_id=bob.id,
        ) as second,
    ):
        assert first.id != second.id
        assert first.workspace.path != second.workspace.path
        (first.workspace.path / "alice.txt").write_text("private work")
        assert not (second.workspace.path / "alice.txt").exists()
    assert not first.workspace.path.exists()
    assert not second.workspace.path.exists()


@pytest.mark.parametrize("error", [RuntimeError, asyncio.CancelledError])
async def test_core_releases_chat_on_failure_and_cancellation(
    in_memory_engine: AsyncEngine, error: type[BaseException]
) -> None:
    app = Octomate()
    thread = await app.threads.ensure(ThreadKey("im", "dm", "alice"))
    with pytest.raises(error):
        async with app.habitats.invoke(
            app.workspaces,
            thread=thread,
            conversation_id=uuid7(),
            owner_id=None,
        ) as invocation:
            raise error()
    assert not invocation.workspace.path.exists()


async def test_core_refuses_anonymous_project_and_managed_dispatch(
    in_memory_engine: AsyncEngine,
    tmp_path: Path,
) -> None:
    root = tmp_path / "project"
    root.mkdir()
    project = a_project(root)
    app = Octomate(workspaces=WorkspaceManager(projects=await a_registry(project)))
    thread = await app.threads.ensure(
        ThreadKey("im", "thread", "room", "turn"),
        project=project,
    )
    with pytest.raises(UnregisteredProjectError):
        async with app.habitats.invoke(
            app.workspaces,
            thread=thread,
            conversation_id=uuid7(),
            owner_id=None,
        ):
            pytest.fail("An anonymous project invocation must not start")
    app.habitats.config = SbxHabitatConfig(image="sandbox@sha256:" + "a" * 64)
    with pytest.raises(HabitatError, match="Managed habitat dispatch"):
        async with app.habitats.invoke(
            app.workspaces,
            thread=thread,
            conversation_id=uuid7(),
            owner_id=(await a_user()).id,
        ):
            pytest.fail("Managed execution must not fall back to native")


async def test_core_checkpoints_an_empty_project_without_a_git_index(
    in_memory_engine: AsyncEngine,
    tmp_path: Path,
) -> None:
    root = tmp_path / "empty"
    root.mkdir()
    project = a_project(root)
    app = Octomate(workspaces=WorkspaceManager(projects=await a_registry(project)))
    thread = await app.threads.ensure(
        ThreadKey("im", "thread", "room", "turn"),
        project=project,
    )
    async with app.habitats.invoke(
        app.workspaces,
        thread=thread,
        conversation_id=uuid7(),
        owner_id=(await a_user()).id,
    ) as invocation:
        (invocation.workspace.path / ".git" / "index").unlink(missing_ok=True)
    assert await app.workspaces.saved(invocation.workspace.path)
