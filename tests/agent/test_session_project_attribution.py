"""OCTO-31 — a native session's thread carries the project it ran in.

Both ingests resolve the hook's own `cwd` through the registry when they create the
session's thread, so sessions started in several repos stop being anonymous siblings.

Neither runtime registers what it finds (OCTO-45): every project is declared, so a
session running where no project claims is filed under none — where it ran is recorded
on the run either way.

The project is an attribute of the thread, never part of its key: nothing about thread
identity changes, and no existing row is revisited.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from pathlib import Path

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine

from octomate import Octomate
from octomate.managers.workspaces import WorkspaceManager
from octomate.schemas.thread import ThreadKey
from octomate.tentacles.agents.claude.hooks import ClaudeHookInput
from octomate.tentacles.agents.claude.ingest import CLAUDE_NATIVE_ID, ClaudeHookIngest
from octomate.tentacles.agents.claude.tailer import ClaudeTranscriptTailer
from octomate.tentacles.agents.codex.hooks import CodexHookInput
from octomate.tentacles.agents.codex.ingest import CODEX_NATIVE_ID, CodexHookIngest
from octomate.tentacles.agents.codex.tailer import CodexTranscriptTailer
from tests.agent.test_codex_native_ingest import stream_rollout
from tests.support.managers import a_project, a_registry


@pytest.fixture(autouse=True)
async def _db(in_memory_engine: AsyncEngine) -> None:
    return


def repo(path: Path) -> Path:
    """A root need not exist, but resolving one compares resolved paths, so a test
    about resolution makes it real."""
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
        ThreadKey(CLAUDE_NATIVE_ID, "thread", session_id)
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
        ThreadKey(CODEX_NATIVE_ID, "thread", session_id)
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
        workspaces=WorkspaceManager(
            projects=await a_registry(a_project(inky), a_project(kraken))
        )
    )

    assert await claude_session(octomate, "sess-inky", inky / "octomate") == "inky"
    assert await claude_session(octomate, "sess-kraken", kraken) == "kraken"


async def test_a_claude_session_outside_every_project_registers_nothing(
    tmp_path: Path,
) -> None:
    # Claude's own projects are a context store keyed by directory, so a session
    # running somewhere is not evidence that somewhere is a tree being worked in.
    # The thread stays unfiled and the run still records where it ran.
    elsewhere = repo(tmp_path / "elsewhere")
    octomate = Octomate(
        workspaces=WorkspaceManager(
            projects=await a_registry(a_project(repo(tmp_path / "inky")))
        )
    )

    assert await claude_session(octomate, "sess-elsewhere", elsewhere) == ""
    assert [project.name for project in octomate.projects.list()] == ["inky"]


async def test_a_codex_session_outside_every_project_registers_nothing(
    tmp_path: Path,
) -> None:
    # OCTO-45: every project is declared, so a directory nobody wrote down stays
    # unregistered — the thread is unfiled, and the run still records where it ran.
    elsewhere = repo(tmp_path / "elsewhere")
    octomate = Octomate(
        workspaces=WorkspaceManager(
            projects=await a_registry(a_project(repo(tmp_path / "inky")))
        )
    )

    assert await codex_session(octomate, "sess-elsewhere", elsewhere) == ""
    assert [project.name for project in octomate.projects.list()] == ["inky"]


async def test_a_session_below_a_declared_root_does_not_register_a_second_project(
    tmp_path: Path,
) -> None:
    # The run is simply below its project's root, which is where runs are allowed to
    # be; every package becoming its own project is the failure here.
    inky = repo(tmp_path / "inky")
    octomate = Octomate(
        workspaces=WorkspaceManager(projects=await a_registry(a_project(inky)))
    )

    assert await claude_session(octomate, "sess-deep", inky / "octomate") == "inky"
    assert [project.name for project in octomate.projects.list()] == ["inky"]


async def test_a_hook_carrying_no_cwd_is_unattributed(tmp_path: Path) -> None:
    # `Path("")` is the process's own directory, so an unguarded resolve would
    # attribute every session to whatever project Octomate was started in.
    octomate = Octomate(
        workspaces=WorkspaceManager(projects=await a_registry(a_project(Path.cwd())))
    )

    assert await claude_session(octomate, "sess-no-cwd", "") == ""


async def test_both_runtimes_file_under_the_same_declared_project(
    tmp_path: Path,
) -> None:
    # Both sessions are in the same directory, so both are in the same project — one
    # registry, whichever runtime asks. The thread binding is frozen, and here it
    # never needed to change.
    inky = repo(tmp_path / "inky")
    octomate = Octomate(
        workspaces=WorkspaceManager(projects=await a_registry(a_project(inky)))
    )

    first = await codex_session(octomate, "sess-codex", inky)
    second = await claude_session(octomate, "sess-claude", inky / "octomate")

    assert first == "inky"
    assert second == "inky"
    octomate.thread_manager.threads.clear()
    reloaded = await octomate.thread_manager.ensure(
        ThreadKey(CLAUDE_NATIVE_ID, "thread", "sess-claude")
    )
    attributed = await reloaded.project
    assert attributed is not None
    assert attributed.name == "inky"


async def test_a_codex_session_is_attributed_the_same_way(tmp_path: Path) -> None:
    inky = repo(tmp_path / "inky")
    octomate = Octomate(
        workspaces=WorkspaceManager(projects=await a_registry(a_project(inky)))
    )

    assert await codex_session(octomate, "codex-inky", inky / "app") == "inky"
    assert await codex_session(octomate, "codex-out", repo(tmp_path / "out")) == ""
    assert [project.name for project in octomate.projects.list()] == ["inky"]


async def test_a_thread_cannot_be_re_attributed(tmp_path: Path) -> None:
    # The workspace a thread names is where every conversation in it runs, and an
    # external session's history is full of absolute paths: a thread that changed
    # project would resume its sessions into a tree they were never written for.
    # The rule is the manager's rather than the field's, because `project_id` has
    # to be settable once for a chat thread to become a working one, and `frozen`
    # cannot say once.
    inky = repo(tmp_path / "inky")
    kraken = repo(tmp_path / "kraken")
    octomate = Octomate(
        workspaces=WorkspaceManager(
            projects=await a_registry(a_project(inky), a_project(kraken))
        )
    )
    thread = await octomate.thread_manager.ensure(
        ThreadKey("im", "thread", "chat", "t1"),
        project=octomate.projects.get("inky"),
    )
    kraken_project = octomate.projects.get("kraken")
    assert kraken_project is not None

    with pytest.raises(ValueError, match="binds once"):
        await octomate.thread_manager.bind(thread.id, kraken_project)


async def test_a_thread_cannot_name_a_project_that_is_not_there(
    tmp_path: Path,
) -> None:
    # Attribution is a reference into the registry, not a label: foreign keys are
    # enforced, so a thread cannot claim a project with no row.
    octomate = Octomate(
        workspaces=WorkspaceManager(
            projects=await a_registry(a_project(repo(tmp_path / "x")))
        )
    )
    unregistered = a_project(repo(tmp_path / "ghost"))

    with pytest.raises(IntegrityError):
        await octomate.thread_manager.ensure(
            ThreadKey("im", "thread", "chat", "t-ghost"), project=unregistered
        )


async def test_attribution_does_not_touch_thread_identity(tmp_path: Path) -> None:
    # The project is an attribute, never part of the key — a session stays keyed
    # by its id, so re-binding one could never strand its history.
    inky = repo(tmp_path / "inky")
    octomate = Octomate(
        workspaces=WorkspaceManager(projects=await a_registry(a_project(inky)))
    )

    await claude_session(octomate, "sess-keyed", inky)
    thread = await octomate.thread_manager.ensure(
        ThreadKey(CLAUDE_NATIVE_ID, "thread", "sess-keyed")
    )

    assert thread.key == ThreadKey(CLAUDE_NATIVE_ID, "thread", "sess-keyed")
    attributed = await thread.project
    assert attributed is not None
    assert attributed.name == "inky"


async def end_session(octomate: Octomate, session_id: str, cwd: Path | str) -> None:
    """`SessionEnd` and nothing before it — Octomate came up in the middle of this
    session, so the per-turn hooks that would have filed its thread never reached
    it. The hook still creates the thread: it is the last event carrying a cwd, and
    a backfill tail attaching later would otherwise create it unfiled."""
    tailer = ClaudeTranscriptTailer(octomate.conversations, octomate.thread_manager)
    ingest = ClaudeHookIngest(octomate, tailer)
    await ingest.handle(
        ClaudeHookInput.model_validate(
            {
                "hook_event_name": "SessionEnd",
                "session_id": session_id,
                "cwd": str(cwd),
            }
        )
    )


async def filed_under(octomate: Octomate, session_id: str) -> str:
    """The name of the project a Claude session's thread is filed under, or ""."""
    thread = await octomate.thread_manager.ensure(
        ThreadKey(CLAUDE_NATIVE_ID, "thread", session_id)
    )
    project = await thread.project
    return project.name if project is not None else ""


async def test_a_session_ending_before_any_hook_is_still_filed(
    tmp_path: Path,
) -> None:
    # `SessionEnd` carries a cwd like every other hook, and this is the last moment it
    # can be used: the hook creates the thread, and a native session never binds
    # one afterwards, so one born unfiled would stay unfiled.
    inky = repo(tmp_path / "inky")
    octomate = Octomate(
        workspaces=WorkspaceManager(projects=await a_registry(a_project(inky)))
    )

    await end_session(octomate, "sess-recovered", inky / "octomate")

    assert await filed_under(octomate, "sess-recovered") == "inky"


async def test_a_session_ending_outside_every_project_is_filed_nowhere(
    tmp_path: Path,
) -> None:
    # Filing, not registering: the same rule the per-turn hooks follow. A directory no
    # project holds leaves the thread unfiled, and the run still records where it ran.
    inky = repo(tmp_path / "inky")
    octomate = Octomate(
        workspaces=WorkspaceManager(projects=await a_registry(a_project(inky)))
    )

    await end_session(octomate, "sess-elsewhere", repo(tmp_path / "elsewhere"))

    assert await filed_under(octomate, "sess-elsewhere") == ""
    assert [project.name for project in octomate.projects.list()] == ["inky"]


def codex_rollout(path: Path, cwd: Path, workspace_roots: Sequence[Path]) -> None:
    """A rollout whose turn declares a multi-directory workspace, as Codex writes it."""
    records: list[dict[str, object]] = [
        {
            "timestamp": "2026-08-09T10:00:00Z",
            "type": "session_meta",
            "payload": {"session_id": "codex-ws", "cwd": str(cwd)},
        },
        {
            "timestamp": "2026-08-09T10:00:01Z",
            "type": "event_msg",
            "payload": {"type": "task_started", "turn_id": "turn-1"},
        },
        {
            "timestamp": "2026-08-09T10:00:02Z",
            "type": "turn_context",
            "payload": {
                "turn_id": "turn-1",
                "cwd": str(cwd),
                "workspace_roots": [str(root) for root in workspace_roots],
            },
        },
    ]
    path.write_text("".join(json.dumps(record) + "\n" for record in records))


async def tail_rollout(octomate: Octomate, rollout: Path) -> None:
    """Stream one rollout the way production reaches it: a tail attaches and feeds
    the file's framed lines."""
    tailer = CodexTranscriptTailer(octomate.conversations, octomate.thread_manager)
    await stream_rollout(tailer, "codex-ws", rollout)


async def test_workspace_roots_register_nothing(tmp_path: Path) -> None:
    # A turn's workspace names the directories a session may work in; naming is not
    # declaring (OCTO-45), so tailing it grows no registry.
    inky, kraken = repo(tmp_path / "inky"), repo(tmp_path / "kraken")
    rollout = tmp_path / "rollout.jsonl"
    codex_rollout(rollout, inky, [inky, kraken])
    octomate = Octomate(workspaces=WorkspaceManager(projects=await a_registry()))

    await tail_rollout(octomate, rollout)

    assert octomate.projects.list() == []
