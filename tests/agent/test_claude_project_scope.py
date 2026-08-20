"""OCTO-36, OCTO-48 — Claude's file writes are scoped to its thread's workspace.

`cwd` is a default Claude can walk out of, so the boundary is enforced in a
PreToolUse hook: a write whose path resolves outside the workspace is denied with a
reason that names it. The hook is registered only for a run whose thread is in a
project, since that is the run that has a workspace; an unscoped run is untouched.

The boundary moved with the run. It was the project's roots while runs happened in
`project.root`; now a run happens in a fork of that project's mirror, and the
person's own checkout is one of the places it may not write.
"""

from __future__ import annotations

from functools import partial
from pathlib import Path
from typing import cast

import pytest
from claude_agent_sdk import HookContext, HookInput
from claude_agent_sdk.types import HookCallback
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import AsyncEngine

from octomate import Octomate
from octomate.config.agents import ClaudeCodeConfig
from octomate.managers.project import ProjectManager
from octomate.schemas.conversation import ChannelAddress
from octomate.schemas.project import DirectoryUpstream, Project
from octomate.schemas.thread import ThreadKey
from octomate.tentacles.agents.claude import ClaudeCodeTentacle
from octomate.tentacles.agents.claude import base as claude_base
from octomate.tentacles.agents.claude.base import (
    WRITE_TOOL_PATHS,
    deny_outside_workspace,
)
from tests.support.agents import CLAUDE_MODELS, RecordingClaudeClient
from tests.support.managers import FakeConversationManager, a_registry

KEY = ChannelAddress(
    channel_tentacle_id="im", chat_type="dm", chat_id="alice", user_id="alice"
)
HOOK_SECRET = SecretStr("test-hook-secret")


@pytest.fixture(autouse=True)
async def _db(in_memory_engine: AsyncEngine) -> None:
    return


@pytest.fixture(autouse=True)
def _client(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(claude_base, "ClaudeSDKClient", RecordingClaudeClient)


def a_project(root: Path, *extra_roots: Path) -> Project:
    """A registered project over directories that exist, as a declared root must."""
    root.mkdir(parents=True, exist_ok=True)
    for extra in extra_roots:
        extra.mkdir(parents=True, exist_ok=True)
    return Project(
        root=root,
        extra_roots=list(extra_roots),
        upstream=DirectoryUpstream(path=root),
    )


def a_write(tool_name: str, path: str | Path) -> HookInput:
    """The PreToolUse input the CLI sends for one of Claude's file-writing tools."""
    return cast(
        HookInput,
        {
            "hook_event_name": "PreToolUse",
            "tool_name": tool_name,
            "tool_input": {WRITE_TOOL_PATHS[tool_name]: str(path)},
            "tool_use_id": "t1",
        },
    )


async def denial(hook: HookCallback, tool_name: str, path: str | Path) -> str | None:
    """A boundary's reason for refusing this write, or None if it let it through."""
    output = await hook(a_write(tool_name, path), "t1", cast(HookContext, {}))
    specific = cast(dict[str, dict[str, str]], output).get("hookSpecificOutput")
    if specific is None:
        return None
    assert specific["permissionDecision"] == "deny"
    return specific["permissionDecisionReason"]


async def refusal(workspace: Path, tool_name: str, path: str | Path) -> str | None:
    """The same question, of the boundary a run in `workspace` would register."""
    return await denial(partial(deny_outside_workspace, workspace), tool_name, path)


def a_workspace(tmp_path: Path) -> Path:
    """A workspace directory as `WorkspaceManager` hands one over: absolute, and
    resolved, since the hook compares against it without resolving it again."""
    workspace = tmp_path / "workspaces" / "t1"
    workspace.mkdir(parents=True, exist_ok=True)
    return workspace.resolve()


async def test_a_write_inside_the_workspace_is_allowed(tmp_path: Path) -> None:
    workspace = a_workspace(tmp_path)

    assert await refusal(workspace, "Write", workspace / "octomate" / "new.py") is None


async def test_an_edit_outside_the_workspace_is_denied_naming_it(
    tmp_path: Path,
) -> None:
    workspace = a_workspace(tmp_path)
    outside = tmp_path / "kraken" / "base.py"

    reason = await refusal(workspace, "Edit", outside)

    # The model has to be able to report a blocker instead of retrying the path.
    assert reason is not None
    assert str(workspace) in reason
    assert str(outside) in reason


async def test_a_write_into_the_projects_own_checkout_is_denied(
    tmp_path: Path,
) -> None:
    # The boundary that moved: the project's root is where a person works, and a
    # run reaches it only through what its thread commits.
    project = a_project(tmp_path / "inky")

    assert (
        await refusal(
            a_workspace(tmp_path), "Write", project.root / "octomate" / "x.py"
        )
        is not None
    )


async def test_a_path_escaping_with_dotdot_is_denied(tmp_path: Path) -> None:
    # `is_relative_to` is lexical, so the check resolves before it compares.
    workspace = a_workspace(tmp_path)
    escaping = workspace / ".." / "t2" / "base.py"

    assert await refusal(workspace, "Edit", escaping) is not None


async def test_a_path_escaping_through_a_symlink_is_denied(tmp_path: Path) -> None:
    workspace = a_workspace(tmp_path)
    (tmp_path / "kraken").mkdir()
    (workspace / "sibling").symlink_to(tmp_path / "kraken")

    escaping = workspace / "sibling" / "base.py"

    assert await refusal(workspace, "Edit", escaping) is not None


async def test_a_notebook_edit_is_scoped_by_its_own_path_key(tmp_path: Path) -> None:
    assert (
        await refusal(a_workspace(tmp_path), "NotebookEdit", tmp_path / "n.ipynb")
        is not None
    )


async def test_a_write_naming_no_path_decides_nothing(tmp_path: Path) -> None:
    malformed = cast(
        HookInput,
        {"hook_event_name": "PreToolUse", "tool_name": "Write", "tool_input": {}},
    )

    decision = await deny_outside_workspace(
        a_workspace(tmp_path), malformed, "t1", cast(HookContext, {})
    )

    assert decision == {}


async def a_run(
    projects: ProjectManager,
    *,
    declared: str = "",
    configured: str = "/configured",
) -> tuple[list[str | None], HookCallback | None, Path]:
    """Drive one run and answer with the PreToolUse matchers it handed the SDK, the
    scope hook it registered — the boundary itself, callable, so a test asks it
    rather than inspecting how it was bound — and the directory it ran in. `declared`
    names the project the run's thread is in; a thread in none runs at `configured`."""
    octomate = Octomate(conversations=FakeConversationManager(), projects=projects)
    thread = await octomate.thread_manager.ensure(
        ThreadKey("im", "thread", "c", "t1"),
        project=projects.get(declared) if declared else None,
    )
    tentacle = ClaudeCodeTentacle(
        "claude",
        octomate,
        config=ClaudeCodeConfig(models=set(CLAUDE_MODELS), cwd=configured),
        hook_secret=HOOK_SECRET,
    )
    async with tentacle.run_stream_events(
        "do it", conversation_address=KEY, thread_id=thread.id, run_name="react"
    ) as stream:
        async for _event in stream:
            pass
    options = RecordingClaudeClient.last_options
    assert options is not None
    assert options.hooks is not None
    hooks = options.hooks["PreToolUse"]
    scope = next(
        (
            matcher.hooks[0]
            for matcher in hooks
            if matcher.matcher == "|".join(WRITE_TOOL_PATHS)
        ),
        None,
    )
    return [matcher.matcher for matcher in hooks], scope, Path(options.cwd or "")


async def test_a_run_in_a_project_registers_the_scope_hook(tmp_path: Path) -> None:
    inky = a_project(tmp_path / "inky").root

    matchers, scope, cwd = await a_run(
        await a_registry(a_project(inky)), declared="inky"
    )

    assert matchers == ["AskUserQuestion", "Write|Edit|NotebookEdit"]
    assert scope is not None
    assert await denial(scope, "Edit", tmp_path / "elsewhere" / "x.py") is not None
    assert await denial(scope, "Edit", cwd / "base.py") is None


async def test_the_boundary_is_the_workspace_not_either_checkout(
    tmp_path: Path,
) -> None:
    # A run is scoped to where it landed, so where it runs and what bounds it can
    # never disagree — and neither project's own checkout is where it landed.
    inky = a_project(tmp_path / "inky").root
    kraken = a_project(tmp_path / "kraken").root
    projects = await a_registry(a_project(inky), a_project(kraken))

    _matchers, scope, cwd = await a_run(
        projects, declared="kraken", configured=str(inky)
    )

    assert scope is not None
    assert cwd.parent == (tmp_path / ".octomate" / "workspaces")
    assert await denial(scope, "Edit", cwd / "base.py") is None
    assert await denial(scope, "Edit", kraken / "base.py") is not None
    reason = await denial(scope, "Edit", inky / "base.py")
    assert reason is not None
    assert str(cwd) in reason


async def test_a_run_in_no_project_is_unaffected(tmp_path: Path) -> None:
    inky = a_project(tmp_path / "inky").root
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()

    # Belonging to no project, and configured to a directory no project claims.
    matchers, scope, cwd = await a_run(
        await a_registry(a_project(inky)), configured=str(elsewhere)
    )

    # A thread in no project has no workspace, so nothing scopes it — and it runs
    # where it always did.
    assert matchers == ["AskUserQuestion"]
    assert scope is None
    assert cwd == elsewhere


async def test_with_nothing_declared_no_run_is_scoped() -> None:
    matchers, scope, _cwd = await a_run(await a_registry())

    assert (matchers, scope) == (["AskUserQuestion"], None)
