"""OCTO-32 — a thread's project is where both agents run.

The thread is the only place a project is declared: every conversation belongs to
one, so a run asks its thread rather than carrying a copy. With nothing declared,
both agents dispatch exactly where they did before, down to not reading the thread.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path

import pytest
from claude_agent_sdk import ClaudeAgentOptions
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import AsyncEngine

from octomate import Octomate
from octomate.config.agents import ClaudeCodeConfig, CodexConfig
from octomate.managers.project import ProjectManager
from octomate.schemas.conversation import ChannelAddress
from octomate.schemas.project import Project
from octomate.schemas.thread import Thread, ThreadKey
from octomate.tentacles.agents.claude import ClaudeCodeTentacle
from octomate.tentacles.agents.claude import base as claude_base
from octomate.tentacles.agents.codex import CodexTentacle
from octomate.tentacles.agents.codex import base as codex_base
from tests.agent.test_codex_tentacle import FakeCodex, reset_fake_codex, text_script
from tests.support.agents import RecordingClaudeClient
from tests.support.managers import FakeConversationManager

KEY = ChannelAddress(
    channel_tentacle_id="im", chat_type="private", chat_id="alice", user_id="alice"
)
HOOK_SECRET = SecretStr("test-hook-secret")


@pytest.fixture(autouse=True)
async def _db(in_memory_engine: AsyncEngine) -> None:
    return


async def registry(spec: Sequence[Mapping[str, object]]) -> ProjectManager:
    manager = ProjectManager(
        [Project.shell(Project.Create.model_validate(entry)) for entry in spec]
    )
    await manager.reconcile()
    return manager


def repo(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    return path


async def a_thread(octomate: Octomate, chat_id: str, project: str = "") -> Thread:
    """A persisted thread, optionally in a declared project — both its thread row and
    the project it references have to exist, since each is a real foreign key."""
    return await octomate.thread_manager.ensure(
        ThreadKey("im", "private", chat_id, ""),
        project=octomate.projects.get(project) if project else None,
    )


async def claude_run(octomate: Octomate, thread: Thread) -> ClaudeAgentOptions:
    """Drive one Claude run and answer with the options it handed the SDK."""
    tentacle = ClaudeCodeTentacle(
        "claude",
        octomate,
        config=ClaudeCodeConfig(cwd="/configured"),
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
        config=CodexConfig(approval_mode="deny_all", cwd="/configured"),
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


async def test_a_thread_in_a_project_runs_claude_in_its_root(tmp_path: Path) -> None:
    inky = repo(tmp_path / "inky")
    octomate = Octomate(
        conversations=FakeConversationManager(),
        projects=await registry([{"root": str(inky)}]),
    )

    options = await claude_run(octomate, await a_thread(octomate, "chat", "inky"))

    assert options.cwd == str(inky)


async def test_a_thread_in_a_project_runs_codex_in_its_root(tmp_path: Path) -> None:
    inky = repo(tmp_path / "inky")
    octomate = Octomate(
        conversations=FakeConversationManager(),
        projects=await registry([{"root": str(inky)}]),
    )

    thread = await a_thread(octomate, "chat", "inky")

    assert await codex_run(octomate, thread) == str(inky)


async def test_a_project_extra_root_is_reachable(tmp_path: Path) -> None:
    inky, settings = repo(tmp_path / "inky"), repo(tmp_path / "settings")
    octomate = Octomate(
        conversations=FakeConversationManager(),
        projects=await registry([{"root": str(inky), "extra_roots": [str(settings)]}]),
    )

    options = await claude_run(octomate, await a_thread(octomate, "chat", "inky"))

    assert options.add_dirs == [str(settings)]


async def test_a_second_run_in_the_same_conversation_stays_in_the_root(
    tmp_path: Path,
) -> None:
    # Resuming resolves the same way as starting: the project belongs to the thread,
    # so a conversation that already exists is not a path around it.
    inky = repo(tmp_path / "inky")
    octomate = Octomate(
        conversations=FakeConversationManager(),
        projects=await registry([{"root": str(inky)}]),
    )
    thread = await a_thread(octomate, "chat", "inky")

    await claude_run(octomate, thread)
    options = await claude_run(octomate, thread)

    assert options.cwd == str(inky)


async def test_a_thread_naming_an_undeclared_project_falls_back(
    tmp_path: Path,
) -> None:
    octomate = Octomate(
        conversations=FakeConversationManager(),
        projects=await registry([{"root": str(repo(tmp_path / "inky"))}]),
    )

    # The registry is what YAML currently declares; a name it no longer carries has
    # no root to run in.
    options = await claude_run(octomate, await a_thread(octomate, "chat", "retired"))

    assert options.cwd == "/configured"
