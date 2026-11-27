"""Inkling approval/resume and workspace boundaries with real SRT, no LLM calls."""

from collections.abc import AsyncIterator
from dataclasses import replace
from pathlib import Path

import pytest
from pydantic_ai.workspaces import WorkspaceRef
from uuid_utils.compat import uuid7

from octomate.habitat.srt import SrtHabitat
from octomate.managers.habitat import Invocation
from octomate.schemas.conversation import Conversation
from octomate.schemas.volumes import Volume
from octomate.schemas.volumes.storage import HostDirectory
from octomate.schemas.workspaces import Workspace
from octomate.tentacles.inkling.workspace import HabitatWorkspaceBackend
from tests.habitat.agents.test_inkling import (
    test_approved_project_tools_run_in_the_habitat as test_approved_project_tools_run_in_the_habitat,
)


@pytest.fixture
async def inkling_habitat(
    habitat: SrtHabitat, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[SrtHabitat]:
    scopes: list[SrtHabitat] = []
    prepare = SrtHabitat.prepare

    async def prepare_scope(self: SrtHabitat) -> None:
        await prepare(self)
        scopes.append(self)

    monkeypatch.setattr(SrtHabitat, "prepare", prepare_scope)
    yield habitat
    assert len(scopes) == 2
    assert scopes[0] is not scopes[1]
    for scoped in scopes:
        assert scoped.root == (tmp_path / "guest").resolve()
        assert not scoped.processes
        assert scoped.executor is not None
        assert scoped.executor.closed
        assert scoped.scratch is not None
        assert not scoped.scratch.exists()
    assert habitat.executor is None


@pytest.mark.parametrize("read_only", [False, True])
async def test_scoped_backend_restricts_files_and_commands(
    habitat: SrtHabitat, tmp_path: Path, read_only: bool
) -> None:
    work = tmp_path / "work"
    work.mkdir()
    peer = tmp_path / "peer"
    peer.mkdir()
    (peer / "secret").write_text("peer-secret")
    (work / "escape").symlink_to(peer, target_is_directory=True)
    thread = uuid7()
    invocation = Invocation(
        id=uuid7(),
        habitat=habitat,
        workspace=Workspace(
            thread_id=thread,
            volumes={
                "work": Volume(owner_id=habitat.user_id, storage=HostDirectory(work)),
                "config": Volume(owner_id=habitat.user_id, storage=HostDirectory(peer)),
                "cache": Volume(owner_id=habitat.user_id, storage=HostDirectory(peer)),
            },
        ),
        conversation=Conversation(thread_id=thread, agent_tentacle_id="inkling"),
    )
    async with habitat.scope(work, read_only=read_only) as scoped:
        backend = HabitatWorkspaceBackend(replace(invocation, habitat=scoped))
        assert backend.ref == WorkspaceRef(
            provider="octomate", id=f"{habitat.user_id}/{invocation.volume.id}"
        )
        with pytest.raises(PermissionError):
            await backend.read_bytes(str(peer / "secret"))
        with pytest.raises(PermissionError):
            await backend.write_bytes(str(work / "escape/proof"), b"escape")
        denied = await backend.run(["/bin/cat", str(peer / "secret")])
        assert denied.exit_code != 0
        write = await backend.run("printf proof > proof", shell=True)
        assert (write.exit_code != 0) == read_only
        if read_only:
            with pytest.raises(PermissionError):
                await backend.write_bytes(str(work / "file"), b"proof")
        else:
            await backend.write_bytes(str(work / "file"), b"proof")
            assert await backend.read_bytes(str(work / "file")) == b"proof"
    assert not (peer / "proof").exists()
    assert invocation.habitat is habitat
