"""OCTO-31 — a native session's thread carries the project it ran in.

Both ingests resolve the hook's own `cwd` through the registry when they create the
session's thread, so sessions started in several repos stop being anonymous
siblings. The project is an attribute of the thread, never part of its key: nothing
about thread identity changes, and no existing row is revisited.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine

from octomate import Octomate
from octomate.managers.project import ProjectManager
from octomate.schemas.project import Project
from octomate.schemas.thread import ThreadKey
from octomate.tentacles.agents.claude.hooks import ClaudeHookInput
from octomate.tentacles.agents.claude.ingest import CLAUDE_NATIVE_ID, ClaudeHookIngest
from octomate.tentacles.agents.claude.tailer import ClaudeTranscriptTailer
from octomate.tentacles.agents.codex.hooks import CodexHookInput
from octomate.tentacles.agents.codex.ingest import CODEX_NATIVE_ID, CodexHookIngest
from octomate.tentacles.agents.codex.tailer import CodexTranscriptTailer


@pytest.fixture(autouse=True)
async def _db(in_memory_engine: AsyncEngine) -> None:
    return


async def registry(spec: Sequence[Mapping[str, object]]) -> ProjectManager:
    """The registry as startup leaves it, from what `projects:` declares."""
    manager = ProjectManager(
        [Project.shell(Project.Create.model_validate(entry)) for entry in spec]
    )
    await manager.reconcile()
    return manager


def repo(path: Path) -> Path:
    """A declared root has to exist, so a test that declares one makes it."""
    path.mkdir(parents=True, exist_ok=True)
    return path


async def claude_session(octomate: Octomate, session_id: str, cwd: Path | str) -> str:
    """Ingest one native Claude turn, and answer with its thread's project."""
    tailer = ClaudeTranscriptTailer(octomate.conversations, octomate.thread_manager)
    ingest = ClaudeHookIngest(octomate, tailer)
    await ingest.handle(
        ClaudeHookInput.model_validate(
            {
                "hook_event_name": "UserPromptSubmit",
                "session_id": session_id,
                "cwd": str(cwd),
                "prompt": "hi",
                "prompt_id": "p1",
            }
        )
    )
    thread = await octomate.thread_manager.ensure(
        ThreadKey(CLAUDE_NATIVE_ID, "private", session_id, "")
    )
    project = await thread.project
    return project.name if project is not None else ""


async def codex_session(octomate: Octomate, session_id: str, cwd: Path | str) -> str:
    """Ingest one native Codex turn, and answer with its thread's project."""
    tailer = CodexTranscriptTailer(octomate.conversations, octomate.thread_manager)
    ingest = CodexHookIngest(octomate, tailer)
    await ingest.handle(
        CodexHookInput.model_validate(
            {
                "hook_event_name": "UserPromptSubmit",
                "session_id": session_id,
                "cwd": str(cwd),
                "prompt": "hi",
                "turn_id": "t1",
            }
        )
    )
    thread = await octomate.thread_manager.ensure(
        ThreadKey(CODEX_NATIVE_ID, "private", session_id, "")
    )
    project = await thread.project
    return project.name if project is not None else ""


async def test_two_sessions_in_two_repos_carry_different_projects(
    tmp_path: Path,
) -> None:
    # Neither client is configured for any of this; the cwd its hook already sends
    # is the whole input.
    inky, kraken = repo(tmp_path / "inky"), repo(tmp_path / "kraken")
    octomate = Octomate(
        projects=await registry([{"root": str(inky)}, {"root": str(kraken)}])
    )

    assert await claude_session(octomate, "sess-inky", inky / "octomate") == "inky"
    assert await claude_session(octomate, "sess-kraken", kraken) == "kraken"


async def test_a_session_where_no_project_is_declared_is_unattributed(
    tmp_path: Path,
) -> None:
    octomate = Octomate(
        projects=await registry([{"root": str(repo(tmp_path / "inky"))}])
    )

    assert (
        await claude_session(octomate, "sess-elsewhere", tmp_path / "elsewhere") == ""
    )


async def test_a_hook_carrying_no_cwd_is_unattributed(tmp_path: Path) -> None:
    # `Path("")` is the process's own directory, so an unguarded resolve would
    # attribute every session to whatever project Octomate was started in.
    octomate = Octomate(projects=await registry([{"root": str(Path.cwd())}]))

    assert await claude_session(octomate, "sess-no-cwd", "") == ""


async def test_declaring_a_project_later_leaves_old_threads_alone(
    tmp_path: Path,
) -> None:
    inky = repo(tmp_path / "inky")
    octomate = Octomate(projects=await registry([]))

    before = await claude_session(octomate, "sess-before", inky)
    octomate.projects = await registry([{"root": str(inky)}])
    after = await claude_session(octomate, "sess-after", inky)

    assert before == ""
    assert after == "inky"
    # The thread that started unattributed stays that way: its project is a fact
    # about where its work began, not a lookup redone on every hook.
    octomate.thread_manager.threads.clear()
    reloaded = await octomate.thread_manager.ensure(
        ThreadKey(CLAUDE_NATIVE_ID, "private", "sess-before", "")
    )
    assert await reloaded.project is None


async def test_a_codex_session_is_attributed_the_same_way(tmp_path: Path) -> None:
    inky = repo(tmp_path / "inky")
    octomate = Octomate(projects=await registry([{"root": str(inky)}]))

    assert await codex_session(octomate, "codex-inky", inky / "app") == "inky"
    assert await codex_session(octomate, "codex-out", tmp_path / "elsewhere") == ""


async def test_a_thread_cannot_name_a_project_that_is_not_there(
    tmp_path: Path,
) -> None:
    # Attribution is a reference into the registry, not a label: foreign keys are
    # enforced, so a thread cannot claim a project with no row.
    octomate = Octomate(projects=await registry([{"root": str(repo(tmp_path / "x"))}]))
    unregistered = Project(root=repo(tmp_path / "ghost"))

    with pytest.raises(IntegrityError):
        await octomate.thread_manager.ensure(
            ThreadKey("im", "private", "sess-ghost", ""), project=unregistered
        )


async def test_attribution_does_not_touch_thread_identity(tmp_path: Path) -> None:
    # The project is an attribute, never part of the key — a session stays keyed
    # by its id, so re-binding one could never strand its history.
    inky = repo(tmp_path / "inky")
    octomate = Octomate(projects=await registry([{"root": str(inky)}]))

    await claude_session(octomate, "sess-keyed", inky)
    thread = await octomate.thread_manager.ensure(
        ThreadKey(CLAUDE_NATIVE_ID, "private", "sess-keyed", "")
    )

    assert thread.key == ThreadKey(CLAUDE_NATIVE_ID, "private", "sess-keyed", "")
    attributed = await thread.project
    assert attributed is not None
    assert attributed.name == "inky"
