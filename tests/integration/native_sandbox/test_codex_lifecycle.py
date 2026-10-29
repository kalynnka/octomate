"""The native workspace profile survives warm turns and app-server restarts."""

from pathlib import Path
from shlex import quote

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from octomate import Octomate
from octomate.managers.workspaces import WorkspaceManager
from octomate.schemas.thread import ThreadKey
from octomate.types.permissions import CodexPermissionMode
from tests.integration.native_sandbox.matrix import SandboxTree
from tests.integration.native_sandbox.runners import codex_runner
from tests.support.managers import a_project, a_registry


async def test_codex_profile_survives_mode_changes_and_resume(
    sandbox_tree: SandboxTree, tmp_path: Path, in_memory_engine: AsyncEngine
) -> None:
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
    source = sandbox_tree.outside / "canary.txt"
    assert source.read_text() == sandbox_tree.marker
    command = (
        f"cat input.txt; if cat {quote(str(source))} > /dev/null; "
        "then printf OUTSIDE_ALLOWED; else printf OUTSIDE_DENIED; fi"
    )
    conversation = await octomate.conversations.ensure(
        thread.id, agent_tentacle_id="codex"
    )
    modes: tuple[CodexPermissionMode, ...] = (
        "user_review",
        "full_access",
        "auto_review",
    )
    session_id: str | None = None
    async with codex_runner(octomate, thread.id, tmp_path) as call:
        for mode in modes:
            await octomate.conversations.set_permission_mode(conversation, mode)
            outcome = await call("shell", command)
            assert sandbox_tree.marker in outcome.text, outcome.text
            allowed = mode == "full_access"
            assert ("OUTSIDE_ALLOWED" in outcome.text) == allowed, outcome.text
            assert ("OUTSIDE_DENIED" in outcome.text) == (not allowed), outcome.text
            conversation = await octomate.conversations.get(conversation.id)
            if session_id is None:
                session_id = conversation.external_id
            assert conversation.external_id == session_id
    async with codex_runner(octomate, thread.id, tmp_path) as call:
        outcome = await call("shell", command)
        assert sandbox_tree.marker in outcome.text, outcome.text
        assert "OUTSIDE_DENIED" in outcome.text, outcome.text
        assert "OUTSIDE_ALLOWED" not in outcome.text, outcome.text
        conversation = await octomate.conversations.get(conversation.id)
        assert conversation.external_id == session_id
    (tmp_path / "codex-home/config.toml").write_text(
        '[permissions.octomate-workspace.filesystem]\n":root"="write"\n'
    )
    with pytest.raises(
        ValueError, match="native workspace permission profile was overridden"
    ):
        async with codex_runner(octomate, thread.id, tmp_path) as call:
            await call("shell", command)
