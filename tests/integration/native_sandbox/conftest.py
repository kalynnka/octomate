"""Disposable user-data canaries and actual Octomate runtime entry points."""

from collections.abc import AsyncIterator, Iterator
from importlib.metadata import version
from pathlib import Path
from platform import platform
from tempfile import TemporaryDirectory

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine
from uuid_utils.compat import uuid7

from octomate import Octomate
from octomate.managers.workspaces import WorkspaceManager
from octomate.schemas.thread import ThreadKey
from tests.integration.native_sandbox.matrix import (
    SERVER_CANARY,
    NativeCall,
    SandboxTree,
)
from tests.integration.native_sandbox.runners import (
    claude_runner,
    codex_runner,
    deepseek_runner,
    inkling_runner,
)
from tests.support.managers import a_project, a_registry


@pytest.fixture
def sandbox_tree(
    tmp_path: Path, request: pytest.FixtureRequest
) -> Iterator[SandboxTree]:
    if not request.config.getoption("--native-sandbox"):
        pytest.skip("requires --native-sandbox")
    # /tmp is writable in several native presets; using it for user-data canaries
    # would measure temporary-storage exceptions instead of outside-path policy.
    with TemporaryDirectory(prefix=".octomate-sandbox-test-", dir=Path.home()) as root:
        base = Path(root).resolve()
        workspace, outside = base / "workspace", base / "outside"
        shared = tmp_path / "shared-temp"
        for directory in (
            workspace / "bin",
            workspace / ".agents/skills/report",
            outside,
            shared,
        ):
            directory.mkdir(parents=True)
        marker = f"synthetic-{uuid7()}"
        for path in (
            workspace / "input.txt",
            outside / "canary.txt",
            shared / "canary.txt",
        ):
            path.write_text(marker)
        (workspace / ".agents/skills/report/SKILL.md").write_text(
            "---\nname: report\ndescription: Read the synthetic report.\n---\n"
            f"Run `./bin/report input.txt`. Fixture: {marker}\n"
        )
        cli = workspace / "bin/report"
        cli.write_text('#!/bin/sh\ncat "$1"\n')
        cli.chmod(0o755)
        program = outside / "program"
        program.write_text(f"#!/bin/sh\nprintf %s {marker}\n")
        program.chmod(0o755)
        (workspace / "outside-link").symlink_to(outside, target_is_directory=True)
        yield SandboxTree(workspace, outside, shared, marker)


@pytest.fixture(params=("inkling", "codex", "claude", "deepseek"))
async def native_call(
    request: pytest.FixtureRequest,
    sandbox_tree: SandboxTree,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    in_memory_engine: AsyncEngine,
) -> AsyncIterator[NativeCall]:
    distributions = {
        "inkling": "pydantic-ai-harness",
        "codex": "openai-codex",
        "claude": "claude-agent-sdk",
    }
    request.node.user_properties.append(("host", platform()))
    if request.param in distributions:
        distribution = distributions[request.param]
        request.node.user_properties.append((distribution, version(distribution)))
    monkeypatch.setenv(SERVER_CANARY, "synthetic-server-only")
    project = a_project(sandbox_tree.workspace, name="sandbox-audit")
    octomate = Octomate(
        workspaces=WorkspaceManager(
            projects=await a_registry(project),
            workspaces_dir=sandbox_tree.workspace.parent / "runs",
        )
    )
    thread = await octomate.threads.ensure(
        ThreadKey("sandbox-audit", "thread", "synthetic", "audit"), project=project
    )
    workspace = octomate.workspaces.open(thread.id, project)
    sandbox_tree.workspace = await workspace.prepare()
    if request.param == "inkling":
        async with inkling_runner(octomate, thread.id) as call:
            yield call
    elif request.param == "codex":
        async with codex_runner(octomate, thread.id, tmp_path) as call:
            yield call
    elif request.param == "claude":
        async with claude_runner(octomate, thread.id, tmp_path, monkeypatch) as call:
            yield call
    else:
        async with deepseek_runner(octomate, thread.id, tmp_path, monkeypatch) as call:
            yield call
