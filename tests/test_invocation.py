from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine
from uuid_utils.compat import uuid7

from octomate.config.habitat import SbxHabitatConfig
from octomate.habitat import Habitat, NativeHabitat
from octomate.habitat.sbx import SbxHabitat
from octomate.invocation import Invocation
from octomate.managers.workspaces.base import WorkspaceManager
from tests.support.managers import a_project, a_registry, a_thread
from tests.support.users import a_user


@pytest.mark.parametrize("registered", [False, True])
async def test_native_invocation_borrows_the_owners_chat_workspace(
    in_memory_engine: AsyncEngine, registered: bool
) -> None:
    owner_id = (await a_user()).id if registered else None
    thread_id = await a_thread()
    workspaces = WorkspaceManager()
    execution: Habitat = NativeHabitat()
    await execution.prepare()
    async with workspaces.open(thread_id, None, owner_id) as workspace:
        invocation = Invocation(
            id=uuid7(),
            conversation_id=uuid7(),
            workspace=workspace,
            execution=execution,
        )
        assert invocation.owner_id == owner_id
        assert invocation.thread_id == thread_id
        assert invocation.workspace is workspace
        assert invocation.workspace.path.is_dir()
        assert invocation.execution is execution

    await execution.stop()
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
            execution=NativeHabitat(),
        )
        assert invocation.owner_id == workspace.volume.owner_id == owner.id
        assert invocation.thread_id == workspace.volume.thread_id


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
            execution=habitat,
        )
        assert invocation.execution is habitat

    prepare.assert_not_called()
    stop.assert_not_called()


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
            execution=habitat,
        )
