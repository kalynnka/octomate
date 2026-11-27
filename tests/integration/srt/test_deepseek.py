"""Real DSH execution services and Web turns with no model credentials."""

import asyncio
import json
import os
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from pydantic import HttpUrl
from sqlalchemy.ext.asyncio import AsyncEngine

from octomate import Octomate
from octomate.habitat.srt import SrtHabitat
from octomate.schemas.thread import ThreadKey
from octomate.tentacles.deepseek.process import DeepseekProcess
from tests.integration.native_sandbox.conftest import sandbox_tree as sandbox_tree
from tests.integration.native_sandbox.matrix import (
    SERVER_CANARY,
    SHELL_CHECKS,
    NativeCall,
    SandboxTree,
)
from tests.integration.native_sandbox.runners import deepseek_runner
from tests.integration.native_sandbox.test_boundaries import (
    test_native_file_tool as test_native_file_tool,
)
from tests.integration.native_sandbox.test_boundaries import (
    test_server_environment_is_hidden as test_server_environment_is_hidden,
)
from tests.integration.native_sandbox.test_boundaries import (
    test_shell_boundary as check_shell_boundary,
)
from tests.support.managers import a_project, a_registry
from tests.support.users import a_user

pytestmark = pytest.mark.timeout(120)


@pytest.fixture
async def dsh_habitat(habitat: SrtHabitat) -> SrtHabitat:
    paths = os.environ.get("SRT_TEST_READ_PATHS", "")
    if paths:
        await habitat.stop()
        habitat.config = habitat.config.model_copy(
            update={"read_paths": [Path(path) for path in paths.split(os.pathsep)]}
        )
        await habitat.prepare()
    return habitat


async def test_native_workspace_services(dsh_habitat: SrtHabitat) -> None:
    executable = os.environ.get("DSH_TEST_EXECUTABLE")
    if executable is None:
        pytest.fail("Set DSH_TEST_EXECUTABLE to the supported DSH executable")
    workspace = dsh_habitat.root / "workspace"
    workspace.mkdir()
    process = DeepseekProcess(
        executable=executable,
        port=0,
        extra_args=[],
        dsh_home=dsh_habitat.root / "config" / ".dsh",
        ready_timeout=30,
        habitat=dsh_habitat,
    )
    patch = process.driven_patch(process.dsh_home)
    config = next(
        entry["insert"][0]["config"] for entry in patch.root if "insert" in entry
    )
    child = await asyncio.create_subprocess_exec(
        "node",
        str(Path(__file__).with_name("fixtures") / "deepseek_workspace.mjs"),
        json.dumps(config),
        str(workspace),
        str(Path(__file__).parents[3] / "octomate/tentacles/deepseek/workspace.mjs"),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await child.communicate()
    finally:
        if child.returncode is None:
            child.kill()
        await child.wait()
    assert child.returncode == 0, stderr.decode()
    assert json.loads(stdout)["passed"] == 18


@pytest.fixture
async def native_call(
    dsh_habitat: SrtHabitat,
    sandbox_tree: SandboxTree,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    in_memory_engine: AsyncEngine,
) -> AsyncIterator[NativeCall]:
    monkeypatch.setenv(SERVER_CANARY, "synthetic-server-only")
    project = a_project(sandbox_tree.workspace, name="sandbox-audit")
    octomate = Octomate()
    octomate.habitats["native"].config = dsh_habitat.config
    octomate.workspaces.projects = await a_registry(project)
    octomate.volumes.host_storage.root = (
        sandbox_tree.workspace.parent / "runs"
    ).resolve()
    thread = await octomate.threads.ensure(
        ThreadKey("sandbox-audit", "thread", "synthetic", "audit"), project=project
    )
    owner = await a_user("sandbox-owner")
    workspace = await octomate.workspaces.resolve(thread.id, project, owner.id)
    sandbox_tree.workspace = await octomate.workspaces.prepare(workspace)
    started: list[DeepseekProcess] = []
    start = DeepseekProcess.start

    async def capture(process: DeepseekProcess) -> HttpUrl:
        started.append(process)
        return await start(process)

    monkeypatch.setattr(DeepseekProcess, "start", capture)
    try:
        async with deepseek_runner(
            octomate, thread.id, tmp_path, monkeypatch, owner_id=owner.id
        ) as call:
            yield call
        assert any(isinstance(process.habitat, SrtHabitat) for process in started)
    finally:
        await octomate.habitats["native"].close()


@pytest.mark.parametrize(
    "check",
    [
        pytest.param(
            check,
            marks=pytest.mark.xfail(
                strict=True,
                reason="Accepted SRT directory metadata access permits cd; file access stays confined",
            ),
        )
        if check == "outside_cwd"
        else check
        for check in SHELL_CHECKS
    ],
)
async def test_shell_boundary(
    check: str, sandbox_tree: SandboxTree, native_call: NativeCall
) -> None:
    await check_shell_boundary(check, sandbox_tree, native_call)
