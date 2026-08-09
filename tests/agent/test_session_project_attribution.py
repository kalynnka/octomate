"""OCTO-31 — a native session's thread carries the project it ran in.

Both ingests resolve the hook's own `cwd` through the registry when they create the
session's thread, so sessions started in several repos stop being anonymous
siblings. A directory no project holds yet becomes one: both runtimes keep their own
workspace per directory, so a session running there is proof that directory is worked
in, and nothing has to be declared first.

The project is an attribute of the thread, never part of its key: nothing about thread
identity changes, and no existing row is revisited.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from pathlib import Path

import pytest
from pydantic import ValidationError
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine

from octomate import Octomate
from octomate.schemas.project import Project
from octomate.schemas.thread import ThreadKey
from octomate.tentacles.agents.claude.hooks import ClaudeHookInput
from octomate.tentacles.agents.claude.ingest import CLAUDE_NATIVE_ID, ClaudeHookIngest
from octomate.tentacles.agents.claude.tailer import ClaudeTranscriptTailer
from octomate.tentacles.agents.codex import tailer as codex_tailer
from octomate.tentacles.agents.codex import transcript as codex_transcript
from octomate.tentacles.agents.codex.hooks import CodexHookInput
from octomate.tentacles.agents.codex.ingest import CODEX_NATIVE_ID, CodexHookIngest
from octomate.tentacles.agents.codex.tailer import CodexTranscriptTailer
from octomate.tentacles.agents.locks import SessionLocks
from tests.support.managers import a_registry


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
    tailer = CodexTranscriptTailer(
        octomate.conversations, octomate.thread_manager, octomate.projects
    )
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
        projects=await a_registry(Project(root=inky), Project(root=kraken))
    )

    assert await claude_session(octomate, "sess-inky", inky / "octomate") == "inky"
    assert await claude_session(octomate, "sess-kraken", kraken) == "kraken"


async def test_a_session_where_no_project_is_declared_registers_one(
    tmp_path: Path,
) -> None:
    # The whole of "which project is this" is the directory the session is in, so a
    # directory nothing has declared is not anonymous — it is a project nobody had
    # written down yet.
    elsewhere = repo(tmp_path / "elsewhere")
    octomate = Octomate(
        projects=await a_registry(Project(root=repo(tmp_path / "inky")))
    )

    assert await claude_session(octomate, "sess-elsewhere", elsewhere) == "elsewhere"

    registered = octomate.projects.get("elsewhere")
    assert registered is not None
    assert registered.root == elsewhere
    assert registered.origin == "claude"


async def test_a_session_below_a_declared_root_does_not_register_a_second_project(
    tmp_path: Path,
) -> None:
    # The run is simply below its project's root, which is where runs are allowed to
    # be; every package becoming its own project is the failure here.
    inky = repo(tmp_path / "inky")
    octomate = Octomate(projects=await a_registry(Project(root=inky)))

    assert await claude_session(octomate, "sess-deep", inky / "octomate") == "inky"
    assert [project.name for project in octomate.projects.list()] == ["inky"]


async def test_a_hook_carrying_no_cwd_is_unattributed(tmp_path: Path) -> None:
    # `Path("")` is the process's own directory, so an unguarded resolve would
    # attribute every session to whatever project Octomate was started in.
    octomate = Octomate(projects=await a_registry(Project(root=Path.cwd())))

    assert await claude_session(octomate, "sess-no-cwd", "") == ""


async def test_a_later_session_joins_the_project_the_first_registered(
    tmp_path: Path,
) -> None:
    # Both sessions are in the same directory, so both are in the same project — the
    # first registered it and the second found it. The thread binding is frozen, and
    # here it never needed to change.
    inky = repo(tmp_path / "inky")
    octomate = Octomate(projects=await a_registry())

    before = await claude_session(octomate, "sess-before", inky)
    after = await claude_session(octomate, "sess-after", inky)

    assert before == "inky"
    assert after == "inky"
    assert [project.name for project in octomate.projects.list()] == ["inky"]
    octomate.thread_manager.threads.clear()
    reloaded = await octomate.thread_manager.ensure(
        ThreadKey(CLAUDE_NATIVE_ID, "thread", "sess-before")
    )
    attributed = await reloaded.project
    assert attributed is not None
    assert attributed.name == "inky"


async def test_a_codex_session_is_attributed_the_same_way(tmp_path: Path) -> None:
    inky = repo(tmp_path / "inky")
    octomate = Octomate(projects=await a_registry(Project(root=inky)))

    assert await codex_session(octomate, "codex-inky", inky / "app") == "inky"
    assert (
        await codex_session(octomate, "codex-out", repo(tmp_path / "elsewhere"))
        == "elsewhere"
    )
    registered = octomate.projects.get("elsewhere")
    assert registered is not None
    assert registered.origin == "codex"


async def test_a_thread_cannot_be_re_attributed(tmp_path: Path) -> None:
    # The root a thread names is where every conversation in it runs, and an
    # external session's history is full of absolute paths: a thread that changed
    # project would resume its sessions into a tree they were never written for.
    inky = repo(tmp_path / "inky")
    octomate = Octomate(projects=await a_registry(Project(root=inky)))
    thread = await octomate.thread_manager.ensure(
        ThreadKey("im", "thread", "chat", "t1"),
        project=octomate.projects.get("inky"),
    )

    with pytest.raises(ValidationError):
        thread.project_id = None


async def test_a_thread_cannot_name_a_project_that_is_not_there(
    tmp_path: Path,
) -> None:
    # Attribution is a reference into the registry, not a label: foreign keys are
    # enforced, so a thread cannot claim a project with no row.
    octomate = Octomate(projects=await a_registry(Project(root=repo(tmp_path / "x"))))
    unregistered = Project(root=repo(tmp_path / "ghost"))

    with pytest.raises(IntegrityError):
        await octomate.thread_manager.ensure(
            ThreadKey("im", "thread", "chat", "t-ghost"), project=unregistered
        )


async def test_attribution_does_not_touch_thread_identity(tmp_path: Path) -> None:
    # The project is an attribute, never part of the key — a session stays keyed
    # by its id, so re-binding one could never strand its history.
    inky = repo(tmp_path / "inky")
    octomate = Octomate(projects=await a_registry(Project(root=inky)))

    await claude_session(octomate, "sess-keyed", inky)
    thread = await octomate.thread_manager.ensure(
        ThreadKey(CLAUDE_NATIVE_ID, "thread", "sess-keyed")
    )

    assert thread.key == ThreadKey(CLAUDE_NATIVE_ID, "thread", "sess-keyed")
    attributed = await thread.project
    assert attributed is not None
    assert attributed.name == "inky"


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
    """Tail one rollout the way production reaches it — `SessionStart` starts the tail,
    which is also what ensures the session's skeleton before the follow task runs."""
    locks = SessionLocks()
    tailer = CodexTranscriptTailer(
        octomate.conversations, octomate.thread_manager, octomate.projects, locks
    )
    ingest = CodexHookIngest(
        octomate, tailer, locks, extra_transcript_roots=(rollout.parent,)
    )
    await ingest.handle(
        CodexHookInput(
            hook_event_name="SessionStart",
            session_id="codex-ws",
            transcript_path=rollout,
        )
    )
    await tailer.pump_session("codex-ws")
    await tailer.shutdown()


async def test_every_workspace_root_becomes_its_own_project(tmp_path: Path) -> None:
    # A Codex workspace is often several repos at once. They are sibling projects, not
    # extra roots of the one the session sits in: folding them together would make a
    # cwd in kraken resolve to inky, and let a run bound to inky write into it.
    inky, kraken = repo(tmp_path / "inky"), repo(tmp_path / "kraken")
    rollout = tmp_path / "rollout.jsonl"
    codex_rollout(rollout, inky, [inky, kraken])
    octomate = Octomate(projects=await a_registry())

    await tail_rollout(octomate, rollout)

    assert {project.name for project in octomate.projects.list()} == {"inky", "kraken"}
    assert octomate.projects.resolve(kraken / "src") == "kraken"


async def test_a_workspace_root_inside_codex_own_tree_is_not_a_project(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Codex puts a per-session visualization cache under its home into the workspace.
    # A runtime's own storage is never a project.
    monkeypatch.setattr(codex_transcript, "CODEX_HOME_DIRS", (tmp_path / ".codex",))
    monkeypatch.setattr(codex_tailer, "CODEX_HOME_DIRS", (tmp_path / ".codex",))
    inky = repo(tmp_path / "inky")
    cache = repo(tmp_path / ".codex" / "visualizations" / "2026" / "08" / "09")
    rollout = tmp_path / "rollout.jsonl"
    codex_rollout(rollout, inky, [inky, cache])
    octomate = Octomate(projects=await a_registry())

    await tail_rollout(octomate, rollout)

    assert {project.name for project in octomate.projects.list()} == {"inky"}
