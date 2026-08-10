"""OCTO-37 — inkling answers inside its thread's project.

Claude and Codex each discover `AGENTS.md`/`CLAUDE.md` natively, from the directory
they dispatch into. Inkling has no directory at all, so which repo it is answering
about is its thread's project and nothing else: the run mounts that project's own
instructions, and records the root it ran in. With no project, the run is what it was
before there were any — no instructions loaded, and no directory claimed.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from pathlib import Path

import pytest
from pydantic_ai.messages import ModelMessage
from pydantic_ai.models.function import AgentInfo, FunctionModel
from pydantic_ai.tools import DeferredToolRequests
from sqlalchemy.ext.asyncio import AsyncEngine

from octomate import Octomate
from octomate.capabilities.harness.agent import Agent
from octomate.schemas.conversation import ChannelAddress
from octomate.schemas.project import Project
from octomate.schemas.runs import AgentRun
from octomate.schemas.thread import Thread, ThreadKey
from octomate.tentacles.agents.inkling import InklingTentacle
from octomate.tentacles.agents.inkling.base import InklingOutput
from octomate.tentacles.agents.inkling.prompts import SYSTEM_PROMPT
from tests.support.managers import a_registry

ADDRESS = ChannelAddress(
    channel_tentacle_id="im", chat_type="dm", chat_id="alice", user_id="alice"
)
STR_OUTPUT: list[type[str] | type[DeferredToolRequests]] = [str, DeferredToolRequests]


@pytest.fixture(autouse=True)
async def _db(in_memory_engine: AsyncEngine) -> None:
    return


@dataclass
class Scripted:
    """An inkling agent over a model that answers "done" and keeps what each run
    sent it — the instructions it was given, and the tools it was offered."""

    instructions: list[str] = field(default_factory=list)
    tools: list[list[str]] = field(default_factory=list)

    def agent(self) -> Agent[None, InklingOutput]:
        async def respond(
            messages: list[ModelMessage], info: AgentInfo
        ) -> AsyncIterator[str]:
            self.instructions.append(info.instructions or "")
            self.tools.append([tool.name for tool in info.function_tools])
            yield "done"

        return Agent(
            FunctionModel(stream_function=respond, model_name="scripted"),
            deps_type=type(None),
            output_type=STR_OUTPUT,
            system_prompt=SYSTEM_PROMPT,
        )


def a_repo(path: Path, instructions: str) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    (path / "AGENTS.md").write_text(instructions)
    return path


async def a_thread(octomate: Octomate, chat_id: str, project: str = "") -> Thread:
    """A persisted thread, optionally in a registered project — both its thread row and
    the project it references have to exist, since each is a real foreign key."""
    return await octomate.thread_manager.ensure(
        ThreadKey("im", "thread", chat_id, "t1"),
        project=octomate.projects.get(project) if project else None,
    )


async def inkling_run(octomate: Octomate, thread: Thread, scripted: Scripted) -> None:
    """Drive one inkling run through the real react graph."""
    tentacle = InklingTentacle("inkling", octomate, agent=scripted.agent())
    await tentacle.run(
        "what is this repo?",
        conversation_address=ADDRESS,
        thread_id=thread.id,
        output_type=STR_OUTPUT,
    )


async def runs_of(octomate: Octomate, thread: Thread) -> list[AgentRun]:
    conversation = await octomate.conversations.ensure(
        thread.id, agent_tentacle_id="inkling"
    )
    return list(conversation.runs)


async def test_a_run_in_a_project_carries_that_project_s_instructions(
    tmp_path: Path,
) -> None:
    inky = a_repo(tmp_path / "inky", "Never commit without asking.")
    octomate = Octomate(projects=await a_registry(Project(root=inky)))
    scripted = Scripted()

    await inkling_run(octomate, await a_thread(octomate, "chat", "inky"), scripted)

    assert "Never commit without asking." in scripted.instructions[-1]


async def test_the_asset_inventory_tool_stays_off(tmp_path: Path) -> None:
    # It answers with the paths of a repo's `.claude`/`.codex` assets, for a model that
    # can go read them. Inkling cannot, so the tool would only spend a slot.
    inky = a_repo(tmp_path / "inky", "Never commit without asking.")
    octomate = Octomate(projects=await a_registry(Project(root=inky)))
    scripted = Scripted()

    await inkling_run(octomate, await a_thread(octomate, "chat", "inky"), scripted)

    assert scripted.tools[-1] == []


async def test_outside_every_project_nothing_is_loaded(tmp_path: Path) -> None:
    # The repo is on disk, and inkling still knows nothing about it: a directory is a
    # project because the registry says so, not because a file sits in it.
    a_repo(tmp_path / "inky", "Never commit without asking.")
    octomate = Octomate()
    scripted = Scripted()

    await inkling_run(octomate, await a_thread(octomate, "chat"), scripted)

    assert scripted.instructions[-1] == ""


async def test_two_projects_never_see_each_other_s_instructions(
    tmp_path: Path,
) -> None:
    inky = a_repo(tmp_path / "inky", "inky is the checkout this instance runs from.")
    kraken = a_repo(tmp_path / "kraken", "kraken is the second checkout.")
    octomate = Octomate(
        projects=await a_registry(Project(root=inky), Project(root=kraken))
    )
    scripted = Scripted()

    await inkling_run(octomate, await a_thread(octomate, "one", "inky"), scripted)
    await inkling_run(octomate, await a_thread(octomate, "two", "kraken"), scripted)

    first, second = scripted.instructions
    assert "inky is" in first
    assert "kraken is" not in first
    assert "kraken is" in second
    assert "inky is" not in second


async def test_nothing_above_the_project_root_is_loaded(tmp_path: Path) -> None:
    # The walk-up is off, so an ancestor's instructions stay out — the operator's own
    # `~/.claude/CLAUDE.md` is what that rule is really protecting.
    (tmp_path / "AGENTS.md").write_text("the operator's private rules")
    inky = a_repo(tmp_path / "inky", "inky's own rules")
    octomate = Octomate(projects=await a_registry(Project(root=inky)))
    scripted = Scripted()

    await inkling_run(octomate, await a_thread(octomate, "chat", "inky"), scripted)

    assert "inky's own rules" in scripted.instructions[-1]
    assert "the operator's private rules" not in scripted.instructions[-1]


async def test_a_disabled_project_reaches_no_run(tmp_path: Path) -> None:
    # A root disk has lost is nowhere to work. The thread keeps the project it was
    # filed under, and the run is one in no project — instructions and all. Another
    # project stays enabled so the registry is not simply empty.
    inky = a_repo(tmp_path / "inky", "Never commit without asking.")
    octomate = Octomate(
        projects=await a_registry(
            Project(root=inky, enabled=False),
            Project(root=a_repo(tmp_path / "kraken", "kraken's rules")),
        )
    )
    scripted = Scripted()
    thread = await a_thread(octomate, "chat", "inky")

    await inkling_run(octomate, thread, scripted)

    assert scripted.instructions[-1] == ""
    [run] = await runs_of(octomate, thread)
    assert run.cwd is None


async def test_a_run_in_a_project_records_its_root(tmp_path: Path) -> None:
    inky = a_repo(tmp_path / "inky", "inky's own rules")
    octomate = Octomate(projects=await a_registry(Project(root=inky)))
    thread = await a_thread(octomate, "chat", "inky")

    await inkling_run(octomate, thread, Scripted())

    [run] = await runs_of(octomate, thread)
    assert run.cwd == inky


async def test_a_run_outside_every_project_records_no_directory() -> None:
    # Inkling has no configured directory to fall back to, so a run in no project
    # names none rather than claiming wherever Octomate was started.
    octomate = Octomate()
    thread = await a_thread(octomate, "chat")

    await inkling_run(octomate, thread, Scripted())

    [run] = await runs_of(octomate, thread)
    assert run.cwd is None
