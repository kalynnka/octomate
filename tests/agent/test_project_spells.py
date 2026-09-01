"""A chat thread says which project it is about, on every driven runtime.

Three operations, all gateway spells: the `projects` facet of `scry`, `teleport`
with a `project`, and `dispel`. They ride the identity the gateway already carries — a registered
user or a visitor, a driven turn or a native session, a thread or none — so the
gate is a refusal in the tool body rather than a tool that comes and goes. The
gateway validates the project and the ref and records the move; binding the thread
is the graph's, on the thread that turns out to be landed in. The ref is resolved
against the mirror *before* any move, because a thread binds once — a branch that
turns out not to exist has to leave the thread free to ask again for the one that
was meant. A `dispel` is recorded the same way and performed by the graph once the
turn is out of the tree.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import cast

import pytest
from fastmcp import Client
from fastmcp.dependencies import Depends
from fastmcp.exceptions import ToolError
from pydantic_ai import CallDeferred, RunContext
from pydantic_ai.exceptions import ModelRetry
from sqlalchemy.ext.asyncio import AsyncEngine

from octomate.capabilities.gateway import GatewayCapability
from octomate.config.mirrors import MirrorsConfig
from octomate.config.users import UserConfig
from octomate.database import async_session
from octomate.managers import ThreadManager, UserManager
from octomate.managers.gateway import GatewayRefusal, GatewaySession
from octomate.managers.workspaces import MirrorManager, WorkspaceManager
from octomate.managers.workspaces.mirrors import run_git
from octomate.mcp.gateway import gateway_mcp
from octomate.schemas.thread import Thread, ThreadKey
from octomate.schemas.triage import HERE_TARGET, ProjectSummary, TeleportDecision
from octomate.schemas.user import UserProfile
from tests.support.managers import FakeThreadManager, a_project, a_registry

CHAT = ThreadKey("im", "thread", "c", "t1")
FAKE_CONTEXT = cast(RunContext[None], None)


@pytest.fixture(autouse=True)
async def _db(in_memory_engine: AsyncEngine) -> None:
    return


@dataclass
class Harness:
    session: GatewaySession
    workspaces: WorkspaceManager
    threads: ThreadManager
    thread: Thread


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


async def a_harness(
    tmp_path: Path, *, registered: bool = True, key: ThreadKey = CHAT
) -> Harness:
    """The spells over real collaborators: the registry is what makes a name a
    project, and the mirror is what a ref has to resolve against. The session is
    one driven turn's, speaking for a registered user unless told otherwise."""
    root = tmp_path / "inky"
    root.mkdir()
    (root / "readme.md").write_text("hello")
    off = tmp_path / "off"
    off.mkdir()
    workspaces = WorkspaceManager(
        projects=await a_registry(a_project(root), a_project(off, enabled=False)),
        mirrors=MirrorManager(config=MirrorsConfig(), mirrors_dir=tmp_path / "mirrors"),
        workspaces_dir=tmp_path / "workspaces",
    )
    users, profile = await a_registered_profile()
    threads = ThreadManager(users=users)
    thread = await threads.ensure(key)
    session = GatewaySession(
        channel_routes={},
        current_agent_id="inkling",
        users=users,
        user_profile=(
            profile
            if registered
            else UserProfile(channel_tentacle_id="im", channel_user_id="U-visitor")
        ),
        thread_id=thread.id,
        threads=threads,
        workspaces=workspaces,
    )
    return Harness(
        session=session, workspaces=workspaces, threads=threads, thread=thread
    )


def inky(harness: Harness) -> Path:
    project = harness.workspaces.projects.get("inky")
    assert project is not None
    return harness.workspaces.mirrors.path(project)


async def test_a_visitor_is_refused_the_projects_and_the_move(tmp_path: Path) -> None:
    # The old gate, as a refusal rather than a tool that comes and goes.
    harness = await a_harness(tmp_path, registered=False)

    with pytest.raises(GatewayRefusal, match="no registered user"):
        await harness.session.scry("projects")
    with pytest.raises(GatewayRefusal, match="no registered user"):
        await harness.session.teleport(
            hint="into inky", destination=HERE_TARGET, project="inky"
        )


async def test_a_registered_user_scries_the_enabled_projects(tmp_path: Path) -> None:
    harness = await a_harness(tmp_path)

    assert await harness.session.scry("projects") == [
        ProjectSummary(name="inky", description=None)
    ]


async def test_a_teleport_into_a_project_validates_it_and_records_the_move(
    tmp_path: Path,
) -> None:
    harness = await a_harness(tmp_path)

    decision = await harness.session.teleport(
        hint="into inky", destination=HERE_TARGET, project="inky"
    )

    assert decision == TeleportDecision(hint="into inky", here=True, project="inky")
    assert harness.session.decision is decision
    # Nothing bound yet: that is the graph's, on the thread it lands in. The mirror
    # is synced here, which is what a ref has to resolve against.
    unbound = await harness.threads.get(harness.thread.id)
    assert unbound is not None
    assert unbound.project_id is None
    assert harness.workspaces.existing(harness.thread.id) is None
    assert inky(harness).is_dir()


async def test_staying_put_without_a_project_is_the_agent_carrying_on(
    tmp_path: Path,
) -> None:
    harness = await a_harness(tmp_path)

    with pytest.raises(GatewayRefusal, match="you carrying on"):
        await harness.session.teleport(hint="staying", destination=HERE_TARGET)


async def test_an_unregistered_project_is_refused_with_the_list(
    tmp_path: Path,
) -> None:
    # Listing what there is, because the model can fix this itself; a disabled
    # project is neither bindable nor listed.
    harness = await a_harness(tmp_path)

    with pytest.raises(GatewayRefusal, match=r"Available: inky\."):
        await harness.session.teleport(
            hint="h", destination=HERE_TARGET, project="kraken"
        )
    with pytest.raises(GatewayRefusal, match=r"Available: inky\."):
        await harness.session.teleport(hint="h", destination=HERE_TARGET, project="off")


async def test_a_ref_that_does_not_resolve_is_refused_before_any_move(
    tmp_path: Path,
) -> None:
    # The ordering that matters: a thread binds once, so checking the ref after
    # the move would spend the one bind on a branch that was never there.
    harness = await a_harness(tmp_path)

    with pytest.raises(GatewayRefusal, match="no 'nope' to start from"):
        await harness.session.teleport(
            hint="h", destination=HERE_TARGET, project="inky", ref="nope"
        )

    assert harness.session.decision is None
    # And the branch that was meant can still be named.
    await run_git("branch", "feat/theirs", cwd=inky(harness))
    decision = await harness.session.teleport(
        hint="h", destination=HERE_TARGET, project="inky", ref="feat/theirs"
    )
    assert decision.ref == "feat/theirs"


async def test_only_a_thread_binds_and_a_dm_is_told_to_open_one(
    tmp_path: Path,
) -> None:
    # Refused before the mirror is even synced: a DM or a group outlives every
    # project in it, and the way out is a sub-thread — `destination` `thread`.
    harness = await a_harness(tmp_path, key=ThreadKey("im", "dm", "u1"))

    with pytest.raises(GatewayRefusal, match="`destination` `thread`"):
        await harness.session.teleport(
            hint="h", destination=HERE_TARGET, project="inky"
        )

    assert not inky(harness).exists()


async def test_a_thread_binds_once(tmp_path: Path) -> None:
    harness = await a_harness(tmp_path)
    project = harness.workspaces.projects.get("inky")
    assert project is not None
    await harness.threads.bind(harness.thread.id, project)

    with pytest.raises(GatewayRefusal, match="binds once"):
        await harness.session.teleport(
            hint="h", destination=HERE_TARGET, project="inky"
        )


async def test_a_bound_threads_agent_may_dispel_its_workspace(tmp_path: Path) -> None:
    # Recorded, not done: the release is the graph's once the turn is out of the
    # tree. No registered user is needed — a release costs a fork, never work.
    harness = await a_harness(tmp_path, registered=False)
    project = harness.workspaces.projects.get("inky")
    assert project is not None
    await harness.threads.bind(harness.thread.id, project)

    sentence = await harness.session.dispel()

    assert harness.session.dispelling
    assert sentence.startswith("Releasing this thread's workspace when this turn ends")


async def test_a_thread_about_no_project_has_nothing_to_dispel(tmp_path: Path) -> None:
    harness = await a_harness(tmp_path)

    with pytest.raises(GatewayRefusal, match="about no project"):
        await harness.session.dispel()

    assert not harness.session.dispelling


async def test_a_native_session_may_list_but_neither_move_nor_dispel(
    tmp_path: Path,
) -> None:
    harness = await a_harness(tmp_path)
    harness.session.native = True

    assert await harness.session.scry("projects") == [
        ProjectSummary(name="inky", description=None)
    ]
    with pytest.raises(GatewayRefusal, match="lives in your terminal"):
        await harness.session.teleport(
            hint="h", destination=HERE_TARGET, project="inky"
        )
    with pytest.raises(GatewayRefusal, match="lives in your terminal"):
        await harness.session.dispel()


async def test_a_gateway_built_without_the_managers_is_a_wiring_bug() -> None:
    users, profile = await a_registered_profile()
    session = GatewaySession(
        channel_routes={}, current_agent_id="inkling", users=users, user_profile=profile
    )

    with pytest.raises(RuntimeError):
        await session.scry("projects")
    with pytest.raises(RuntimeError):
        await session.teleport(hint="h", destination=HERE_TARGET, project="inky")
    with pytest.raises(RuntimeError):
        await session.dispel()


async def test_inkling_hears_a_refusal_as_a_retry(tmp_path: Path) -> None:
    harness = await a_harness(tmp_path, registered=False)
    capability = GatewayCapability(session=harness.session)

    with pytest.raises(ModelRetry, match="no registered user"):
        await capability.scry(FAKE_CONTEXT, "projects")
    with pytest.raises(ModelRetry, match="no registered user"):
        await capability.teleport(FAKE_CONTEXT, "into inky", HERE_TARGET, "inky")
    with pytest.raises(ModelRetry, match="about no project"):
        await capability.dispel(FAKE_CONTEXT)


async def test_inkling_defers_the_move_for_the_graph_to_perform(tmp_path: Path) -> None:
    harness = await a_harness(tmp_path)
    capability = GatewayCapability(session=harness.session)

    with pytest.raises(CallDeferred) as deferred:
        await capability.teleport(FAKE_CONTEXT, "into inky", HERE_TARGET, "inky")

    assert deferred.value.metadata == {
        "kind": "teleport",
        "hint": "into inky",
        "channel": "",
        "user": "",
        "here": True,
        "project": "inky",
        "ref": "",
    }
    # Nothing bound yet: that is the graph's, on the thread it lands in.
    unbound = await harness.threads.get(harness.thread.id)
    assert unbound is not None
    assert unbound.project_id is None


async def test_an_mcp_runtime_reads_the_list_and_hears_a_refusal_as_a_tool_error(
    tmp_path: Path,
) -> None:
    harness = await a_harness(tmp_path)
    server = gateway_mcp(Depends(lambda: harness.session), FakeThreadManager())

    async with Client(server) as client:
        listed = await client.call_tool("scry", {"reveal": "projects"})
        with pytest.raises(ToolError, match=r"Available: inky\."):
            await client.call_tool(
                "teleport",
                {"hint": "h", "destination": {"kind": "here"}, "project": "kraken"},
            )
        with pytest.raises(ToolError, match="about no project"):
            await client.call_tool("dispel", {})
        project = harness.workspaces.projects.get("inky")
        assert project is not None
        await harness.threads.bind(harness.thread.id, project)
        dispelled = await client.call_tool("dispel", {})

    assert listed.data == "- inky"
    assert harness.session.dispelling
    assert str(dispelled.data).startswith("Releasing this thread's workspace")
