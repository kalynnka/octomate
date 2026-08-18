"""OCTO-36 — Claude's file writes are scoped to its project's roots.

`cwd` is a default Claude can walk out of, so the boundary is enforced in a
PreToolUse hook: a write whose path resolves outside the project's roots is denied
with a reason that names the project. The hook is registered only for a run whose
directory — the one its thread's project put it in — resolves to a declared project,
so an unscoped run is untouched.
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
from octomate.tentacles.agents.claude.base import WRITE_TOOL_PATHS, deny_outside_project
from tests.support.agents import RecordingClaudeClient
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


async def refusal(project: Project, tool_name: str, path: str | Path) -> str | None:
    """The same question, of the boundary a run bound to `project` would register."""
    return await denial(partial(deny_outside_project, project), tool_name, path)


async def test_a_write_inside_the_root_is_allowed(tmp_path: Path) -> None:
    project = a_project(tmp_path / "inky")

    assert (
        await refusal(project, "Write", tmp_path / "inky" / "octomate" / "new.py")
        is None
    )


async def test_an_edit_outside_the_roots_is_denied_naming_the_project(
    tmp_path: Path,
) -> None:
    project = a_project(tmp_path / "inky")
    outside = tmp_path / "kraken" / "base.py"

    reason = await refusal(project, "Edit", outside)

    # The model has to be able to report a blocker instead of retrying the path.
    assert reason is not None
    assert "inky" in reason
    assert str(outside) in reason


async def test_an_edit_inside_an_extra_root_is_allowed(tmp_path: Path) -> None:
    project = a_project(tmp_path / "octoview", tmp_path / "Code" / "User")
    settings = tmp_path / "Code" / "User" / "settings.json"

    assert await refusal(project, "Edit", settings) is None


async def test_a_path_escaping_with_dotdot_is_denied(tmp_path: Path) -> None:
    # `is_relative_to` is lexical, so the check resolves before it compares.
    project = a_project(tmp_path / "inky")
    escaping = tmp_path / "inky" / ".." / "kraken" / "base.py"

    assert await refusal(project, "Edit", escaping) is not None


async def test_a_path_escaping_through_a_symlink_is_denied(tmp_path: Path) -> None:
    project = a_project(tmp_path / "inky")
    (tmp_path / "kraken").mkdir()
    (tmp_path / "inky" / "sibling").symlink_to(tmp_path / "kraken")

    escaping = tmp_path / "inky" / "sibling" / "base.py"

    assert await refusal(project, "Edit", escaping) is not None


async def test_a_symlinked_root_still_contains_its_own_files(tmp_path: Path) -> None:
    # The root itself may be a symlink (a home directory often is). Both sides
    # resolve, so the project still contains what lives under it.
    real = tmp_path / "real" / "inky"
    real.mkdir(parents=True)
    linked = tmp_path / "inky"
    linked.symlink_to(real)
    project = a_project(linked)

    assert await refusal(project, "Write", linked / "octomate" / "new.py") is None


async def test_a_notebook_edit_is_scoped_by_its_own_path_key(tmp_path: Path) -> None:
    project = a_project(tmp_path / "inky")

    assert (
        await refusal(project, "NotebookEdit", tmp_path / "kraken" / "n.ipynb")
        is not None
    )


async def test_a_write_naming_no_path_decides_nothing(tmp_path: Path) -> None:
    project = a_project(tmp_path / "inky")
    malformed = cast(
        HookInput,
        {"hook_event_name": "PreToolUse", "tool_name": "Write", "tool_input": {}},
    )

    decision = await deny_outside_project(
        project, malformed, "t1", cast(HookContext, {})
    )

    assert decision == {}


async def a_run(
    projects: ProjectManager,
    *,
    declared: str = "",
    configured: str = "/configured",
) -> tuple[list[str | None], HookCallback | None]:
    """Drive one run and answer with the PreToolUse matchers it handed the SDK, and
    the scope hook it registered — the boundary itself, callable, so a test asks it
    rather than inspecting how it was bound. `declared` names the project the run's
    thread is in; a thread in none runs at `configured`."""
    octomate = Octomate(conversations=FakeConversationManager(), projects=projects)
    thread = await octomate.thread_manager.ensure(
        ThreadKey("im", "thread", "c", "t1"),
        project=projects.get(declared) if declared else None,
    )
    tentacle = ClaudeCodeTentacle(
        "claude",
        octomate,
        config=ClaudeCodeConfig(cwd=configured),
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
    return [matcher.matcher for matcher in hooks], scope


async def test_a_run_in_a_project_registers_the_scope_hook(tmp_path: Path) -> None:
    inky = a_project(tmp_path / "inky").root

    matchers, scope = await a_run(await a_registry(a_project(inky)), declared="inky")

    assert matchers == ["AskUserQuestion", "Write|Edit|NotebookEdit"]
    assert scope is not None
    assert await denial(scope, "Edit", tmp_path / "elsewhere" / "x.py") is not None


async def test_the_boundary_follows_the_thread_not_the_configured_directory(
    tmp_path: Path,
) -> None:
    # A run dispatched away from the configured directory is scoped to where it
    # landed, so where it runs and what bounds it can never disagree.
    inky = a_project(tmp_path / "inky").root
    kraken = a_project(tmp_path / "kraken").root
    projects = await a_registry(a_project(inky), a_project(kraken))

    _matchers, scope = await a_run(projects, declared="kraken", configured=str(inky))

    assert scope is not None
    assert await denial(scope, "Edit", kraken / "base.py") is None
    reason = await denial(scope, "Edit", inky / "base.py")
    assert reason is not None
    assert "kraken" in reason


async def test_a_run_in_no_project_is_unaffected(tmp_path: Path) -> None:
    inky = a_project(tmp_path / "inky").root
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()

    # Belonging to no project, and configured to a directory no project claims.
    matchers, scope = await a_run(
        await a_registry(a_project(inky)), configured=str(elsewhere)
    )

    # No declared root covers the run's directory, so nothing scopes it.
    assert matchers == ["AskUserQuestion"]
    assert scope is None


async def test_with_nothing_declared_no_run_is_scoped() -> None:
    assert await a_run(await a_registry()) == (["AskUserQuestion"], None)
