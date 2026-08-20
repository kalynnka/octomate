"""OCTO-32, OCTO-48 — a thread's project decides where both agents run.

The thread is the only place a project is declared: every conversation belongs to
one, so a run asks its thread rather than carrying a copy. With nothing declared,
both agents dispatch exactly where they did before, down to not reading the thread.

Where a declared thread lands moved in OCTO-48: not the project's root — the
person's own checkout, shared by everyone — but this thread's fork of it, at
`.octomate/workspaces/<thread_id>`. The project still decides which fork.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from claude_agent_sdk import ClaudeAgentOptions
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import AsyncEngine

from octomate import Octomate
from octomate.config.agents import ClaudeCodeConfig, CodexConfig
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


async def claude_run(octomate: Octomate, thread: Thread) -> ClaudeAgentOptions:
    """Drive one Claude run and answer with the options it handed the SDK."""
    tentacle = ClaudeCodeTentacle(
        "claude",
        octomate,
        config=ClaudeCodeConfig(models=set(CLAUDE_MODELS), cwd="/configured"),
        hook_secret=HOOK_SECRET,
    )
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
