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
from octomate.schemas.conversation import Conversation
from octomate.schemas.thread import ThreadKey
from octomate.schemas.volumes import GitVolume, UnregisteredProjectError
from octomate.schemas.volumes.storage import HostDirectory
from octomate.schemas.workspaces import Workspace
from tests.support.agents import FakeAgent
from tests.support.managers import a_project, a_registry, a_thread, a_workspace_manager
from tests.support.users import a_user


@pytest.mark.parametrize("registered", [False, True])
async def test_native_invocation_borrows_the_owners_chat_workspace(
    in_memory_engine: AsyncEngine, registered: bool
) -> None:
    owner_id = (await a_user()).id if registered else None
    thread_id = await a_thread()
    workspaces = a_workspace_manager()
    habitat: Habitat = NativeHabitat()
    await habitat.prepare()
    async with workspaces.open(thread_id, None, owner_id) as workspace:
        invocation = Invocation(
            id=uuid7(),
            conversation=Conversation(
                thread_id=workspace.thread_id, agent_tentacle_id="inkling"
            ),
            workspace=workspace,
            habitat=habitat,
        )
        assert invocation.owner_id == owner_id
        assert invocation.thread_id == thread_id
        assert invocation.volume.storage is workspaces.host_storage.get(workspace.path)
        assert invocation.workspace.path.is_dir()
        assert invocation.habitat is habitat
        assert invocation.cwd == workspace.path
        assert isinstance(invocation.cwd, Path)

    await habitat.stop()
    assert not invocation.workspace.path.exists()


async def test_a_registered_owners_cache_outlives_their_chat_workspace(
    in_memory_engine: AsyncEngine,
) -> None:
    owner = await a_user()
    workspaces = a_workspace_manager()
    async with workspaces.open(await a_thread(), None, owner.id) as workspace:
        invocation = Invocation(
            id=uuid7(),
            conversation=Conversation(
                thread_id=workspace.thread_id, agent_tentacle_id="inkling"
            ),
            workspace=workspace,
            habitat=NativeHabitat(),
        )
        volume = workspace.volumes.get("cache")
        assert volume is not None
        cache = volume.path
        assert cache.is_dir()
        assert invocation.environment == {
            "UV_CACHE_DIR": str(cache / "uv"),
            "npm_config_cache": str(cache / "npm"),
            "npm_config_store_dir": str(cache / "pnpm"),
        }

    assert not workspace.path.exists()
    assert cache.is_dir()
    async with workspaces.open(await a_thread("later"), None, owner.id) as later:
        kept = later.volumes.get("cache")
        assert kept is not None
        assert kept.path == cache


async def test_an_unregistered_workspace_has_no_cache(
    in_memory_engine: AsyncEngine,
) -> None:
    workspaces = a_workspace_manager()
    async with workspaces.open(await a_thread(), None, None) as workspace:
        invocation = Invocation(
            id=uuid7(),
            conversation=Conversation(
                thread_id=workspace.thread_id, agent_tentacle_id="inkling"
            ),
            workspace=workspace,
            habitat=NativeHabitat(),
        )
        assert list(workspace.volumes) == ["work"]
        assert invocation.environment == {}


async def test_native_invocation_keeps_the_project_workspace_owner(
    in_memory_engine: AsyncEngine, tmp_path: Path
) -> None:
    owner = await a_user()
    root = tmp_path / "project"
    root.mkdir()
    project = a_project(root)
    workspaces = a_workspace_manager(projects=await a_registry(project))
    async with workspaces.open(await a_thread(), project, owner.id) as workspace:
        invocation = Invocation(
            id=uuid7(),
            conversation=Conversation(
                thread_id=workspace.thread_id, agent_tentacle_id="inkling"
            ),
            workspace=workspace,
            habitat=NativeHabitat(),
        )
        assert isinstance(workspace.volume, GitVolume)
        assert invocation.owner_id == workspace.volume.owner_id == owner.id
        assert invocation.thread_id == workspace.volume.thread_id
        assert invocation.cwd == workspace.path


async def test_invocation_borrows_the_habitat_without_managing_its_lifetime(
    in_memory_engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    owner = await a_user()
    workspaces = a_workspace_manager()
    habitat = SbxHabitat(
        config=SbxHabitatConfig(image="sandbox@sha256:" + "a" * 64),
        user_id=owner.id,
        root=workspaces.root(owner.id),
        config_dir=workspaces.host_storage.slot(owner.id, "config"),
    )
    prepare = AsyncMock()
    stop = AsyncMock()
    monkeypatch.setattr(habitat, "prepare", prepare)
    monkeypatch.setattr(habitat, "stop", stop)
    async with workspaces.open(await a_thread(), None, owner.id) as workspace:
        assert isinstance(workspace.volume, GitVolume)
        invocation = Invocation(
            id=uuid7(),
            conversation=Conversation(
                thread_id=workspace.thread_id, agent_tentacle_id="inkling"
            ),
            workspace=workspace,
            habitat=habitat,
        )
        assert invocation.habitat is habitat
        assert isinstance(workspace.volume, GitVolume)
        # Local sbx shows the owner's mounted root at its host path.
        assert invocation.cwd == PurePosixPath(workspace.path)
        # A sandbox is another machine: its agents keep their configuration in
        # the owner's volume, where the kit puts their credential file.
        root = workspaces.root(owner.id)
        assert habitat.config_dir == root / "config"
        assert invocation.environment == {
            "UV_CACHE_DIR": str(root / "cache" / "uv"),
            "npm_config_cache": str(root / "cache" / "npm"),
            "npm_config_store_dir": str(root / "cache" / "pnpm"),
            "CLAUDE_CONFIG_DIR": str(root / "config" / ".claude"),
            "CODEX_HOME": str(root / "config" / ".codex"),
        }
        mirror = await workspace.volume.mirror
        assert mirror is not None
        with pytest.raises(HabitatError, match="owner's own volumes"):
            habitat.path(mirror)

    prepare.assert_not_called()
    stop.assert_not_called()


async def test_project_native_cwd_is_known_before_materialization(
    in_memory_engine: AsyncEngine, tmp_path: Path
) -> None:
    owner = await a_user()
    project = a_project(tmp_path / "project")
    volume = GitVolume(
        storage=HostDirectory(Path("storage") / str(uuid7())),
        owner_id=owner.id,
        usage="tree",
        thread_id=await a_thread(),
        project_id=project.id,
    )
    storage = volume.storage
    assert volume.thread_id is not None
    invocation = Invocation(
        id=uuid7(),
        conversation=Conversation(
            thread_id=volume.thread_id, agent_tentacle_id="inkling"
        ),
        workspace=Workspace(
            thread_id=volume.thread_id,
            project=project,
            volumes={"work": volume},
        ),
        habitat=NativeHabitat(),
    )
    assert invocation.volume.storage is storage
    assert invocation.cwd == volume.storage.path
    assert not storage.path.exists()


@pytest.mark.parametrize("registered", [False, True])
async def test_invocation_rejects_an_unregistered_or_different_habitat_owner(
    in_memory_engine: AsyncEngine, registered: bool
) -> None:
    owner_id = (await a_user()).id if registered else None
    workspaces = a_workspace_manager()
    habitat = SbxHabitat(
        config=SbxHabitatConfig(image="sandbox@sha256:" + "a" * 64),
        user_id=uuid7(),
        root=workspaces.root(uuid7()),
        config_dir=workspaces.root(uuid7()) / "config",
    )
    workspace = await workspaces.resolve(await a_thread(), None, owner_id)
    message = "owners do not match" if registered else "registered user"
    with pytest.raises(ValueError, match=message):
        Invocation(
            id=uuid7(),
            conversation=Conversation(
                thread_id=workspace.thread_id, agent_tentacle_id="inkling"
            ),
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
    conversation = Conversation(thread_id=thread.id, agent_tentacle_id="inkling")
    async with app.habitats.invoke(
        thread=thread,
        conversation=conversation,
        owner_id=owner_id,
    ) as invocation:
        assert invocation.owner_id == owner_id
        assert invocation.thread_id == thread.id
        assert invocation.conversation is conversation
        assert isinstance(invocation.habitat, NativeHabitat)
        assert invocation.cwd == invocation.workspace.path
        assert invocation.workspace.path.is_dir()
    assert not invocation.workspace.path.exists()


@pytest.mark.parametrize(
    ("registered", "project_thread"), [(False, False), (True, False), (True, True)]
)
async def test_core_uses_the_workspace_volume_lifecycle(
    in_memory_engine: AsyncEngine,
    tmp_path: Path,
    registered: bool,
    project_thread: bool,
) -> None:
    app = Octomate()
    owner_id = (await a_user()).id if registered else None
    project = None
    if project_thread:
        root = tmp_path / "project"
        root.mkdir()
        (root / "source.txt").write_text("project contents")
        project = a_project(root)
        app.workspaces.projects = await a_registry(project)
    thread = await app.threads.ensure(
        ThreadKey("im", "thread", "room", "turn"), project=project
    )
    async with app.habitats.invoke(
        thread=thread,
        conversation=Conversation(thread_id=thread.id, agent_tentacle_id="inkling"),
        owner_id=owner_id,
    ) as invocation:
        assert invocation.owner_id == owner_id
        assert invocation.workspace.path.is_dir()
        assert invocation.volume.storage is app.volumes.host_storage.get(
            invocation.workspace.path
        )
        if project_thread:
            assert (
                invocation.workspace.path / "source.txt"
            ).read_text() == "project contents"
        (invocation.workspace.path / "work.txt").write_text("turn contents")

    assert invocation.volume.storage.uses == 0
    if project_thread:
        assert isinstance(invocation.volume, GitVolume)
        assert (invocation.workspace.path / "work.txt").read_text() == "turn contents"
        assert await app.workspaces.saved(invocation.volume)
    else:
        assert not invocation.workspace.path.exists()


async def test_core_stops_native_habitat_when_volume_preparation_fails(
    in_memory_engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = Octomate()
    owner = await a_user()
    thread = await app.threads.ensure(ThreadKey("im", "dm", "alice"))
    stop = AsyncMock()
    monkeypatch.setattr(NativeHabitat, "stop", stop)
    monkeypatch.setattr(
        app.mirrors, "create", AsyncMock(side_effect=RuntimeError("mirror failed"))
    )

    with pytest.raises(RuntimeError, match="mirror failed"):
        async with app.habitats.invoke(
            thread=thread,
            conversation=Conversation(thread_id=thread.id, agent_tentacle_id="inkling"),
            owner_id=owner.id,
        ):
            pytest.fail("An invocation with failed preparation must not start")

    stop.assert_awaited_once()
    volume = await app.volumes.find(owner.id, thread.id, None)
    assert volume is not None
    assert volume.storage.uses == 0
    assert not volume.storage.path.exists()


async def test_core_and_workspace_contexts_share_the_same_storage(
    in_memory_engine: AsyncEngine,
) -> None:
    app = Octomate()
    owner = await a_user()
    thread = await app.threads.ensure(ThreadKey("im", "thread", "room", "turn"))
    async with app.habitats.invoke(
        thread=thread,
        conversation=Conversation(thread_id=thread.id, agent_tentacle_id="inkling"),
        owner_id=owner.id,
    ) as invocation:
        (invocation.workspace.path / "work").write_text("retained")
        async with app.workspaces.open(thread.id, None, owner.id) as workspace:
            assert (
                app.workspaces.host_storage.get(workspace.path)
                is invocation.volume.storage
            )
            assert invocation.volume.storage.uses == 2
            assert (workspace.path / "work").read_text() == "retained"
        assert invocation.volume.storage.uses == 1
        assert invocation.workspace.path.exists()
    assert invocation.volume.storage.uses == 0
    assert not invocation.workspace.path.exists()


async def test_core_isolates_two_owners_in_a_shared_thread(
    in_memory_engine: AsyncEngine,
) -> None:
    app = Octomate()
    alice, bob = await a_user("alice"), await a_user("bob")
    thread = await app.threads.ensure(ThreadKey("im", "thread", "room", "turn"))
    conversation = Conversation(thread_id=thread.id, agent_tentacle_id="inkling")
    async with (
        app.habitats.invoke(
            thread=thread,
            conversation=conversation,
            owner_id=alice.id,
        ) as first,
        app.habitats.invoke(
            thread=thread,
            conversation=conversation,
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
            thread=thread,
            conversation=Conversation(thread_id=thread.id, agent_tentacle_id="inkling"),
            owner_id=None,
        ) as invocation:
            raise error()
    assert not invocation.workspace.path.exists()


async def test_core_refuses_an_anonymous_project(
    in_memory_engine: AsyncEngine,
    tmp_path: Path,
) -> None:
    root = tmp_path / "project"
    root.mkdir()
    project = a_project(root)
    app = Octomate()
    app.workspaces.projects = await a_registry(project)
    thread = await app.threads.ensure(
        ThreadKey("im", "thread", "room", "turn"),
        project=project,
    )
    with pytest.raises(UnregisteredProjectError):
        async with app.habitats.invoke(
            thread=thread,
            conversation=Conversation(thread_id=thread.id, agent_tentacle_id="inkling"),
            owner_id=None,
        ):
            pytest.fail("An anonymous project invocation must not start")


class SandboxAgent(FakeAgent):
    supports_managed_habitat = True


@pytest.mark.parametrize("registered", [False, True])
async def test_sandbox_dispatch_refuses_before_preparing_anything(
    in_memory_engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch, registered: bool
) -> None:
    app = Octomate()
    app.habitats.config = SbxHabitatConfig(image="sandbox@sha256:" + "a" * 64)
    prepare = AsyncMock()
    monkeypatch.setattr(SbxHabitat, "prepare", prepare)
    thread = await app.threads.ensure(ThreadKey("im", "thread", "room", "turn"))
    # The manager refuses an unregistered sender whatever the agent; an agent
    # that cannot run in a sandbox refuses a registered one itself.
    agent = FakeAgent() if registered else SandboxAgent()
    agent.habitats = app.habitats
    message = "cannot run in a sandbox" if registered else "registered user"
    with pytest.raises(HabitatError, match=message):
        async with agent.inhabit(
            thread=thread,
            conversation=Conversation(thread_id=thread.id, agent_tentacle_id="inkling"),
            owner_id=(await a_user()).id if registered else None,
        ):
            pytest.fail("Sandbox execution must not fall back to the host")
    prepare.assert_not_awaited()
    assert not app.habitats.residences
    assert not app.workspaces.volumes_dir.exists()
    assert not app.workspaces.host_storage.mirrors_dir.exists()


async def test_a_capable_agent_leases_the_owners_sandbox(
    in_memory_engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = Octomate()
    app.habitats.config = SbxHabitatConfig(image="sandbox@sha256:" + "a" * 64)
    monkeypatch.setattr(SbxHabitat, "prepare", AsyncMock())
    monkeypatch.setattr(SbxHabitat, "stop", AsyncMock())
    owner = await a_user()
    thread = await app.threads.ensure(ThreadKey("im", "thread", "room", "turn"))
    agent = SandboxAgent()
    agent.habitats = app.habitats
    try:
        async with agent.inhabit(
            thread=thread,
            conversation=Conversation(thread_id=thread.id, agent_tentacle_id="inkling"),
            owner_id=owner.id,
        ) as invocation:
            habitat = invocation.habitat
            assert isinstance(habitat, SbxHabitat)
            assert habitat.user_id == owner.id
            assert habitat.root == app.workspaces.root(owner.id)
            assert invocation.workspace.path.is_dir()
            assert invocation.cwd == PurePosixPath(invocation.workspace.path)
            assert invocation.environment["CLAUDE_CONFIG_DIR"] == str(
                habitat.config_dir / ".claude"
            )
            residence = app.habitats.residences[owner.id]
            assert residence.invocations == {invocation.id}
        assert not residence.invocations
        assert not invocation.workspace.path.exists()
    finally:
        await app.habitats.close()


async def test_core_checkpoints_an_empty_project_without_a_git_index(
    in_memory_engine: AsyncEngine,
    tmp_path: Path,
) -> None:
    root = tmp_path / "empty"
    root.mkdir()
    project = a_project(root)
    app = Octomate()
    app.workspaces.projects = await a_registry(project)
    thread = await app.threads.ensure(
        ThreadKey("im", "thread", "room", "turn"),
        project=project,
    )
    async with app.habitats.invoke(
        thread=thread,
        conversation=Conversation(thread_id=thread.id, agent_tentacle_id="inkling"),
        owner_id=(await a_user()).id,
    ) as invocation:
        (invocation.workspace.path / ".git" / "index").unlink(missing_ok=True)
        assert await app.workspaces.prune(0) == []
        assert invocation.owner_id is not None
        assert not await app.workspaces.dismiss(thread, invocation.owner_id)
    assert isinstance(invocation.volume, GitVolume)
    assert invocation.project is not None
    assert invocation.project.id == project.id
    assert await app.workspaces.saved(invocation.volume)
