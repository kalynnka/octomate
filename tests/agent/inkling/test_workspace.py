"""The habitat adapter executes upstream workspace operations over real stdio."""

import asyncio
import os
from collections.abc import Callable
from pathlib import Path, PurePosixPath
from unittest.mock import patch

import anyio
import pytest
from pydantic_ai.workspaces import (
    Workspace,
    WorkspaceBackend,
    WorkspaceOutputLimitError,
    WorkspaceRef,
    WorkspaceTimeoutError,
    WorkspaceUnavailableError,
)
from pydantic_ai.workspaces.conformance import WorkspaceBackendSuite
from uuid_utils.compat import uuid7

from octomate.config.habitat import SbxHabitatConfig
from octomate.habitat.sbx import SbxHabitat
from octomate.managers.habitat import Invocation
from octomate.schemas.conversation import Conversation
from octomate.schemas.volumes import Volume
from octomate.schemas.volumes.storage import HostDirectory
from octomate.schemas.workspaces import Workspace as VolumeWorkspace
from octomate.tentacles.inkling.workspace import HabitatWorkspaceBackend
from tests.agent.claude.test_habitat import executable as executable
from tests.test_habitat_contracts import IMAGE


@pytest.fixture
def invocation(executable: Path, tmp_path: Path) -> Invocation:
    owner = uuid7()
    thread = uuid7()
    volume = Volume(owner_id=owner, storage=HostDirectory(tmp_path))
    return Invocation(
        id=uuid7(),
        habitat=SbxHabitat(
            config=SbxHabitatConfig(image=IMAGE, executable=str(executable)),
            user_id=owner,
            root=tmp_path,
            config_dir=tmp_path,
        ),
        workspace=VolumeWorkspace(thread_id=thread, volumes={"work": volume}),
        conversation=Conversation(thread_id=thread, agent_tentacle_id="inkling"),
    )


@pytest.fixture
def backend(invocation: Invocation) -> HabitatWorkspaceBackend:
    with patch.object(
        SbxHabitat, "path", return_value=PurePosixPath(invocation.workspace.path)
    ):
        return HabitatWorkspaceBackend(invocation)


class TestWorkspace(WorkspaceBackendSuite):
    @pytest.fixture
    def backend(self, backend: HabitatWorkspaceBackend) -> HabitatWorkspaceBackend:
        return backend

    @pytest.fixture
    def anyio_backend(self) -> str:
        return "asyncio"

    @pytest.fixture
    def attach_backend(
        self, invocation: Invocation, backend: HabitatWorkspaceBackend
    ) -> Callable[[WorkspaceRef], WorkspaceBackend]:
        def attach(ref: WorkspaceRef) -> WorkspaceBackend:
            with patch.object(
                SbxHabitat, "path", return_value=PurePosixPath(backend.cwd)
            ):
                return HabitatWorkspaceBackend(invocation, ref=ref)

        return attach


def test_reference_rejects_other_workspaces(
    backend: HabitatWorkspaceBackend, invocation: Invocation
) -> None:
    with patch.object(SbxHabitat, "path", return_value=PurePosixPath(backend.cwd)):
        attached = HabitatWorkspaceBackend(invocation, ref=backend.ref)
        assert attached.ref == backend.ref
        with pytest.raises(ValueError, match="authorized invocation"):
            HabitatWorkspaceBackend(
                invocation, ref=WorkspaceRef(provider="octomate", id=str(uuid7()))
            )


async def test_timeout_retains_output(backend: HabitatWorkspaceBackend) -> None:
    with pytest.raises(WorkspaceTimeoutError) as failure:
        await backend.run(
            "printf before; printf problem >&2; sleep 30", shell=True, timeout=0.2
        )
    assert failure.value.stdout == "before"
    assert failure.value.stderr == "problem"


async def test_asyncio_cancel_reaps_guest_command(
    backend: HabitatWorkspaceBackend, tmp_path: Path
) -> None:
    task = asyncio.create_task(
        backend.run("echo $$ > command.pid; exec sleep 30", shell=True)
    )
    try:
        with anyio.fail_after(10):
            # A separate process writes this file; it cannot signal an asyncio event.
            while not (tmp_path / "command.pid").exists():  # noqa: ASYNC110
                await asyncio.sleep(0.02)
        pid = int((tmp_path / "command.pid").read_text())
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


async def test_concurrent_commands_and_binary_file(
    backend: HabitatWorkspaceBackend,
) -> None:
    results = await asyncio.gather(
        *(backend.run(["printf", str(index)]) for index in range(3))
    )
    assert [result.stdout for result in results] == ["0", "1", "2"]
    workspace = Workspace(backend)
    path = f"{backend.cwd}/binary"
    content = bytes(range(256)) * 1024
    await workspace.write_bytes(path, content)
    assert await workspace.read_bytes(path) == content


async def test_output_limit_preserves_partial_output(
    backend: HabitatWorkspaceBackend,
) -> None:
    with pytest.raises(WorkspaceOutputLimitError) as failure:
        await backend.run(
            ["python", "-c", "import sys; sys.stdout.write('x' * (11 * 1024 * 1024))"]
        )
    assert failure.value.limit == 10 * 1024 * 1024
    assert failure.value.stdout.startswith("x")


async def test_transport_failure_keeps_diagnostics(
    backend: HabitatWorkspaceBackend, executable: Path, caplog: pytest.LogCaptureFixture
) -> None:
    await anyio.Path(executable).write_text(
        "#!/bin/sh\nprintf 'guest startup failed\\n' >&2\nexit 7\n"
    )
    with pytest.raises(WorkspaceUnavailableError):
        await backend.working_dir()
    assert "guest startup failed" in caplog.text


async def test_environment_is_scoped_and_overridable(
    backend: HabitatWorkspaceBackend, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("WORKSPACE_CORE_SECRET", "must-not-leak")
    backend.env = {"WORKSPACE_SETTING": "base", "WORKSPACE_KEPT": "kept"}
    result = await backend.run(
        'printf "%s/%s/%s" "$WORKSPACE_SETTING" "$WORKSPACE_KEPT" "${WORKSPACE_CORE_SECRET-unset}"',
        shell=True,
        env={"WORKSPACE_SETTING": "override"},
    )
    assert result.stdout == "override/kept/unset"
