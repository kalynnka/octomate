"""OCTO-32, OCTO-48 — a thread's project decides where both agents run.

The thread is the only place a project is declared: every conversation belongs to
one, so a run asks its thread rather than carrying a copy. With nothing declared,
both agents dispatch exactly where they did before, down to not reading the thread.

Where a declared thread lands moved in OCTO-48: not the project's root — the
person's own checkout, shared by everyone — but this thread's fork of it, at
`.octomate/workspaces/<thread_id>`. The project still decides which fork.

And what the turn did there does not stay there: OCTO-51 leaves it on the
thread's ref in the mirror, so the fork is a cache rather than the only copy.
"""

from __future__ import annotations

import logging
import os
import shutil
import time
from pathlib import Path

import pytest
from claude_agent_sdk import ClaudeAgentOptions
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import AsyncEngine

from octomate import Octomate
from octomate.config.agents import ClaudeCodeConfig, CodexConfig
from octomate.managers.mirrors import run_git
from octomate.managers.workspaces import thread_ref
from octomate.schemas.conversation import ChannelAddress
from octomate.schemas.thread import Thread, ThreadKey
from octomate.tentacles.agents.claude import ClaudeCodeTentacle
from octomate.tentacles.agents.claude import base as claude_base
from octomate.tentacles.agents.codex import CodexTentacle
from octomate.tentacles.agents.codex import base as codex_base
from tests.agent.test_codex_tentacle import FakeCodex, reset_fake_codex, text_script
from tests.support.agents import CLAUDE_MODELS, CODEX_MODELS, RecordingClaudeClient
from tests.support.managers import FakeConversationManager, a_project, a_registry

KEY = ChannelAddress(
    channel_tentacle_id="im", chat_type="dm", chat_id="alice", user_id="alice"
)
HOOK_SECRET = SecretStr("test-hook-secret")


@pytest.fixture(autouse=True)
async def _db(in_memory_engine: AsyncEngine) -> None:
    return


def repo(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    return path


async def a_thread(octomate: Octomate, chat_id: str, project: str = "") -> Thread:
    """A persisted thread, optionally in a declared project — both its thread row and
    the project it references have to exist, since each is a real foreign key. A
    platform thread, since only work carries a project."""
    return await octomate.thread_manager.ensure(
        ThreadKey("im", "thread", chat_id, "t1"),
        project=octomate.projects.get(project) if project else None,
    )


def a_claude(octomate: Octomate) -> ClaudeCodeTentacle:
    return ClaudeCodeTentacle(
        "claude",
        octomate,
        config=ClaudeCodeConfig(models=set(CLAUDE_MODELS), cwd="/configured"),
        hook_secret=HOOK_SECRET,
    )


async def claude_run(octomate: Octomate, thread: Thread) -> ClaudeAgentOptions:
    """Drive one Claude run and answer with the options it handed the SDK."""
    tentacle = a_claude(octomate)
    async with tentacle.run_stream_events(
        "do it", conversation_address=KEY, thread_id=thread.id, run_name="react"
    ) as stream:
        async for _event in stream:
            pass
    options = RecordingClaudeClient.last_options
    assert options is not None
    return options


async def codex_run(octomate: Octomate, thread: Thread) -> str | None:
    """Drive one Codex run and answer with the cwd its thread was started in."""
    tentacle = CodexTentacle(
        "codex",
        octomate,
        config=CodexConfig(
            models=set(CODEX_MODELS), permission_mode="deny_all", cwd="/configured"
        ),
        hook_secret=HOOK_SECRET,
    )
    async with tentacle:
        async with tentacle.run_stream_events(
            "do it", conversation_address=KEY, thread_id=thread.id, run_name="react"
        ) as stream:
            async for _event in stream:
                pass
    return FakeCodex.thread_calls[-1].cwd


@pytest.fixture(autouse=True)
def _fakes(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(claude_base, "ClaudeSDKClient", RecordingClaudeClient)
    monkeypatch.setattr(codex_base, "AsyncCodex", FakeCodex)
    reset_fake_codex(text_script("done"))


async def test_with_nothing_declared_claude_dispatches_where_it_always_did() -> None:
    octomate = Octomate(conversations=FakeConversationManager())

    options = await claude_run(octomate, await a_thread(octomate, "chat"))

    assert options.cwd == "/configured"
    assert options.add_dirs == []


async def test_with_nothing_declared_codex_dispatches_where_it_always_did() -> None:
    octomate = Octomate(conversations=FakeConversationManager())

    assert await codex_run(octomate, await a_thread(octomate, "chat")) == "/configured"


async def test_a_thread_in_a_project_runs_claude_in_its_workspace(
    tmp_path: Path,
) -> None:
    inky = repo(tmp_path / "inky")
    (inky / "readme.md").write_text("hello")
    octomate = Octomate(
        conversations=FakeConversationManager(),
        projects=await a_registry(a_project(inky)),
    )
    thread = await a_thread(octomate, "chat", "inky")

    options = await claude_run(octomate, thread)

    workspace = octomate.workspaces.path(thread.id)
    assert options.cwd == str(workspace)
    # A fork of the project, not an empty directory named after the thread.
    assert (workspace / "readme.md").read_text() == "hello"


async def test_a_thread_in_a_project_runs_codex_in_its_workspace(
    tmp_path: Path,
) -> None:
    inky = repo(tmp_path / "inky")
    octomate = Octomate(
        conversations=FakeConversationManager(),
        projects=await a_registry(a_project(inky)),
    )

    thread = await a_thread(octomate, "chat", "inky")

    assert await codex_run(octomate, thread) == str(octomate.workspaces.path(thread.id))


async def test_two_threads_on_one_project_never_share_a_directory(
    tmp_path: Path,
) -> None:
    # The failure this change exists to stop: two colleagues asking at once were two
    # agents in one checkout, where one run's uncommitted work is the other's to lose.
    inky = repo(tmp_path / "inky")
    octomate = Octomate(
        conversations=FakeConversationManager(),
        projects=await a_registry(a_project(inky)),
    )
    one = await a_thread(octomate, "first", "inky")
    other = await a_thread(octomate, "second", "inky")

    first = (await claude_run(octomate, one)).cwd
    second = (await claude_run(octomate, other)).cwd

    assert first is not None
    assert second is not None
    assert first != second
    (Path(first) / "wip.py").write_text("mine")
    assert not (Path(second) / "wip.py").exists()
    # And neither of them is the checkout a person works in.
    assert not (inky / "wip.py").exists()


async def test_a_project_extra_root_is_reachable(tmp_path: Path) -> None:
    inky, settings = repo(tmp_path / "inky"), repo(tmp_path / "settings")
    octomate = Octomate(
        conversations=FakeConversationManager(),
        projects=await a_registry(a_project(inky, extra_roots=[settings])),
    )

    options = await claude_run(octomate, await a_thread(octomate, "chat", "inky"))

    assert options.add_dirs == [str(settings)]


async def test_a_second_run_in_the_same_conversation_stays_in_the_workspace(
    tmp_path: Path,
) -> None:
    # Resuming resolves the same way as starting: the project belongs to the thread,
    # so a conversation that already exists is not a path around it — and the second
    # turn lands in the tree the first one left, work in progress included.
    inky = repo(tmp_path / "inky")
    octomate = Octomate(
        conversations=FakeConversationManager(),
        projects=await a_registry(a_project(inky)),
    )
    thread = await a_thread(octomate, "chat", "inky")

    first = await claude_run(octomate, thread)
    assert first.cwd is not None
    (Path(first.cwd) / "wip.py").write_text("half done")
    options = await claude_run(octomate, thread)

    assert options.cwd == first.cwd
    assert (Path(first.cwd) / "wip.py").read_text() == "half done"


async def test_resuming_a_thread_syncs_nothing_and_forks_nothing(
    tmp_path: Path,
) -> None:
    # A workspace is not re-made from its mirror once it exists, so a later turn
    # pays for neither the fork nor the sync in front of it — which is also what
    # keeps a thread going while its upstream is unreachable.
    inky = repo(tmp_path / "inky")
    (inky / "readme.md").write_text("hello")
    octomate = Octomate(
        conversations=FakeConversationManager(),
        projects=await a_registry(a_project(inky)),
    )
    thread = await a_thread(octomate, "chat", "inky")
    await claude_run(octomate, thread)

    (inky / "late.md").write_text("after the first turn")
    await claude_run(octomate, thread)

    project = octomate.projects.get("inky")
    assert project is not None
    assert not (octomate.mirrors.path(project) / "late.md").exists()


async def test_a_thread_naming_an_undeclared_project_falls_back(
    tmp_path: Path,
) -> None:
    octomate = Octomate(
        conversations=FakeConversationManager(),
        projects=await a_registry(a_project(repo(tmp_path / "inky"))),
    )

    # A thread's project is a reference into the registry; a name the registry does
    # not carry has no root to run in.
    options = await claude_run(octomate, await a_thread(octomate, "chat", "retired"))

    assert options.cwd == "/configured"


async def a_project_thread(tmp_path: Path) -> tuple[Octomate, Thread, Path]:
    """An Octomate with one declared project, and a thread in it."""
    inky = repo(tmp_path / "inky")
    (inky / "readme.md").write_text("hello")
    octomate = Octomate(
        conversations=FakeConversationManager(),
        projects=await a_registry(a_project(inky)),
    )
    return octomate, await a_thread(octomate, "chat", "inky"), inky


def mirror_of(octomate: Octomate, name: str) -> Path:
    project = octomate.projects.get(name)
    assert project is not None
    return octomate.mirrors.path(project)


async def test_a_turn_stamps_its_workspace_as_in_use(tmp_path: Path) -> None:
    # Idleness is measured from the turn that last asked for the workspace, and a
    # turn asks for it here. Nothing else stamps it: a run that edits a file two
    # directories down never touches the directory the sweep looks at, and a fork
    # inherits its date from the mirror.
    octomate, thread, _inky = await a_project_thread(tmp_path)
    await claude_run(octomate, thread)
    await octomate.workspaces.save(thread)
    workspace = octomate.workspaces.path(thread.id)
    long_ago = time.time() - 30 * 24 * 60 * 60
    os.utime(workspace, (long_ago, long_ago))

    await claude_run(octomate, thread)

    assert await octomate.workspaces.prune(idle=3600.0) == []
    assert workspace.is_dir()


async def test_a_finished_turn_leaves_its_work_on_the_threads_ref(
    tmp_path: Path,
) -> None:
    octomate, thread, _inky = await a_project_thread(tmp_path)
    options = await claude_run(octomate, thread)
    assert options.cwd is not None
    (Path(options.cwd) / "work.md").write_text("done")

    await octomate.workspaces.save(thread)

    mirror = mirror_of(octomate, "inky")
    ref = thread_ref(thread.id)
    assert await run_git("show", f"{ref}:work.md", cwd=mirror) == "done"


async def test_a_thread_in_no_project_has_no_work_to_leave(tmp_path: Path) -> None:
    # No project is no workspace, and nowhere to push it to either.
    octomate = Octomate(
        conversations=FakeConversationManager(),
        projects=await a_registry(a_project(repo(tmp_path / "inky"))),
    )
    thread = await a_thread(octomate, "chat")

    await octomate.workspaces.save(thread)

    assert not (tmp_path / ".octomate" / "mirrors").exists()


async def test_a_turn_whose_work_cannot_be_saved_says_so_and_carries_on(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    # The turn already happened and already answered. Failing it now would report a
    # delivered answer as an error, so the loss of the backup is reported instead.
    octomate, thread, _inky = await a_project_thread(tmp_path)
    await claude_run(octomate, thread)
    shutil.rmtree(mirror_of(octomate, "inky"))

    with caplog.at_level(logging.ERROR):
        await octomate.workspaces.save(thread)

    assert "could not be saved" in caplog.text
    assert str(octomate.workspaces.path(thread.id)) in caplog.text
