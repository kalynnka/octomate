"""OCTO-52 — a chat thread says which project it is about, and gets a workspace.

The gate is `for_profile`: a visitor is never offered the tools at all, rather
than being refused by them after calling. The ref is resolved against the mirror
*before* the bind, because a thread binds once — a branch that turns out not to
exist has to leave the thread free to ask again for the one that was meant.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pytest
from pydantic_ai import RunContext, RunUsage
from pydantic_ai.exceptions import ModelRetry
from pydantic_ai.models.test import TestModel
from sqlalchemy.ext.asyncio import AsyncEngine

from octomate.capabilities.projects import ProjectCapability
from octomate.config.mirrors import MirrorsConfig
from octomate.config.users import UserConfig
from octomate.database import async_session
from octomate.managers import ConversationManager, ThreadManager, UserManager
from octomate.managers.workspaces import MirrorManager, WorkspaceManager
from octomate.managers.workspaces.mirrors import run_git
from octomate.schemas.thread import Thread, ThreadKey
from octomate.schemas.user import UserProfile
from tests.support.managers import a_project, a_registry

CHAT = ThreadKey("im", "thread", "c", "t1")


@pytest.fixture(autouse=True)
async def _db(in_memory_engine: AsyncEngine) -> None:
    return


@dataclass
class Harness:
    capability: ProjectCapability
    workspaces: WorkspaceManager
    threads: ThreadManager
    ctx: RunContext[None]
    thread: Thread


async def a_harness(tmp_path: Path) -> Harness:
    """The capability over real collaborators: the registry is what makes a name a
    project, and the mirror is what a ref has to resolve against."""
    root = tmp_path / "inky"
    root.mkdir()
    (root / "readme.md").write_text("hello")
    workspaces = WorkspaceManager(
        projects=await a_registry(a_project(root)),
        mirrors=MirrorManager(config=MirrorsConfig(), mirrors_dir=tmp_path / "mirrors"),
        workspaces_dir=tmp_path / "workspaces",
    )
    threads = ThreadManager(users=UserManager())
    conversations = ConversationManager()
    thread = await threads.ensure(CHAT)
    conversation = await conversations.ensure(thread.id, agent_tentacle_id="inkling")
    return Harness(
        capability=ProjectCapability(workspaces, threads, conversations),
        workspaces=workspaces,
        threads=threads,
        ctx=RunContext(
            deps=None,
            model=TestModel(),
            usage=RunUsage(),
            conversation_id=str(conversation.id),
        ),
        thread=thread,
    )


async def call(harness: Harness, tool: str, **arguments: str) -> str:
    toolset = harness.capability.get_toolset()
    assert toolset is not None
    tools = await toolset.get_tools(harness.ctx)
    return await toolset.call_tool(tool, dict(arguments), harness.ctx, tools[tool])


async def a_registered_profile() -> tuple[UserManager, UserProfile]:
    users = UserManager(
        {
            "someone": UserConfig.model_validate(
                {"profiles": {"im": {"channel_user_id": "U1"}}}
            )
        }
    )
    await users.reconcile()
    async with async_session() as session:
        profile = await session.one_or_none(
            UserProfile, expressions=[UserProfile["channel_user_id"] == "U1"]
        )
    assert profile is not None
    return users, profile


async def test_a_visitor_is_offered_nothing(tmp_path: Path) -> None:
    # The refusal a visitor gets is the absence of the tool. A tool that appeared
    # and then said no is one the model spends a turn arguing with.
    harness = await a_harness(tmp_path)

    visitor = UserProfile(channel_tentacle_id="im", channel_user_id="U-visitor")

    assert await harness.capability.for_profile(visitor) is None


async def test_a_registered_user_is_offered_the_tools(tmp_path: Path) -> None:
    harness = await a_harness(tmp_path)
    users, profile = await a_registered_profile()
    harness.threads.users = users

    assert await harness.capability.for_profile(profile) is harness.capability


async def test_binding_forks_the_workspace_and_says_it_is_next_turn(
    tmp_path: Path,
) -> None:
    harness = await a_harness(tmp_path)

    answer = await call(harness, "work_on_project", name="inky")

    assert "inky" in answer
    # The whole point of the tool result: this run cannot use what it just made.
    assert "next turn" in answer
    bound = await harness.threads.get(harness.thread.id)
    assert bound is not None
    attributed = await bound.project
    assert attributed is not None
    assert attributed.name == "inky"
    # Forked here rather than left for the next turn, so the ref never has to be
    # carried on the row to survive until then.
    workspace = harness.workspaces.existing(harness.thread.id)
    assert workspace is not None
    assert (workspace / "readme.md").read_text() == "hello"


async def test_an_unregistered_project_comes_back_as_a_retry(tmp_path: Path) -> None:
    # Listing what there is, because the model can fix this itself.
    harness = await a_harness(tmp_path)

    with pytest.raises(ModelRetry, match="inky"):
        await call(harness, "work_on_project", name="kraken")


async def test_a_ref_that_does_not_resolve_leaves_the_thread_free(
    tmp_path: Path,
) -> None:
    # The ordering that matters: a thread binds once, so checking the ref after
    # binding would spend the one bind on a branch that was never there.
    harness = await a_harness(tmp_path)

    with pytest.raises(ModelRetry, match="no 'nope' to start from"):
        await call(harness, "work_on_project", name="inky", ref="nope")

    unbound = await harness.threads.get(harness.thread.id)
    assert unbound is not None
    assert unbound.project_id is None
    assert harness.workspaces.existing(harness.thread.id) is None

    # And the thread can still be given the branch that was meant.
    project = harness.workspaces.projects.get("inky")
    assert project is not None
    await run_git("branch", "feat/theirs", cwd=harness.workspaces.mirrors.path(project))

    await call(harness, "work_on_project", name="inky", ref="feat/theirs")

    rebound = await harness.threads.get(harness.thread.id)
    assert rebound is not None
    assert rebound.project_id == project.id
