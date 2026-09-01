"""OCTO-68 — a chat thread says which project it is about, on every driven runtime.

The two operations are gateway spells: the `projects` facet of `scry`, and `bind`.
They ride the identity the gateway already carries — a registered user or a
visitor, a driven turn or a native session, a thread or none — so the gate is a
refusal in the tool body rather than a tool that comes and goes. The ref is
resolved against the mirror *before* the bind, because a thread binds once: a
branch that turns out not to exist has to leave the thread free to ask again for
the one that was meant.
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
from octomate.schemas.triage import BindDecision, ProjectSummary
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


async def test_a_visitor_is_refused_the_projects_and_the_bind(tmp_path: Path) -> None:
    # The old gate, as a refusal rather than a tool that comes and goes.
    harness = await a_harness(tmp_path, registered=False)

    with pytest.raises(GatewayRefusal, match="no registered user"):
        await harness.session.scry("projects")
    with pytest.raises(GatewayRefusal, match="no registered user"):
        await harness.session.bind(project="inky")


async def test_a_registered_user_scries_the_enabled_projects(tmp_path: Path) -> None:
    harness = await a_harness(tmp_path)

    assert await harness.session.scry("projects") == [
        ProjectSummary(name="inky", description=None)
    ]


async def test_binding_forks_the_workspace_and_ends_the_turn(tmp_path: Path) -> None:
    harness = await a_harness(tmp_path)

    answer = await harness.session.bind(project="inky")

    assert "inky" in answer
    # The whole point of the tool result: this run cannot use what it just made,
    # so it ends — recorded as the decision a runtime is interrupted on.
    assert "turn ends here" in answer
    assert harness.session.decision == BindDecision(project="inky")
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


async def test_an_unregistered_project_is_refused_with_the_list(
    tmp_path: Path,
) -> None:
    # Listing what there is, because the model can fix this itself; a disabled
    # project is neither bindable nor listed.
    harness = await a_harness(tmp_path)

    with pytest.raises(GatewayRefusal, match=r"Available: inky\."):
        await harness.session.bind(project="kraken")
    with pytest.raises(GatewayRefusal, match=r"Available: inky\."):
        await harness.session.bind(project="off")


async def test_a_ref_that_does_not_resolve_leaves_the_thread_free(
    tmp_path: Path,
) -> None:
    # The ordering that matters: a thread binds once, so checking the ref after
    # binding would spend the one bind on a branch that was never there.
    harness = await a_harness(tmp_path)

    with pytest.raises(GatewayRefusal, match="no 'nope' to start from"):
        await harness.session.bind(project="inky", ref="nope")

    unbound = await harness.threads.get(harness.thread.id)
    assert unbound is not None
    assert unbound.project_id is None
    assert harness.workspaces.existing(harness.thread.id) is None

    # And the thread can still be given the branch that was meant.
    project = harness.workspaces.projects.get("inky")
    assert project is not None
    await run_git("branch", "feat/theirs", cwd=harness.workspaces.mirrors.path(project))

    await harness.session.bind(project="inky", ref="feat/theirs")

    rebound = await harness.threads.get(harness.thread.id)
    assert rebound is not None
    assert rebound.project_id == project.id


async def test_only_a_thread_binds_and_a_dm_is_told_to_teleport_first(
    tmp_path: Path,
) -> None:
    # Refused before the mirror is even synced: a DM or a group outlives every
    # project in it, and the way out is a sub-thread — which `teleport` opens.
    harness = await a_harness(tmp_path, key=ThreadKey("im", "dm", "u1"))

    with pytest.raises(GatewayRefusal, match="`teleport` into a sub-thread first"):
        await harness.session.bind(project="inky")

    project = harness.workspaces.projects.get("inky")
    assert project is not None
    assert not harness.workspaces.mirrors.path(project).exists()
    assert harness.workspaces.existing(harness.thread.id) is None


async def test_a_thread_binds_once(tmp_path: Path) -> None:
    # The ledger's own refusal, spoken as the gateway's: the tool is always there,
    # so a second bind is told no rather than never offered.
    harness = await a_harness(tmp_path)
    await harness.session.bind(project="inky")

    with pytest.raises(GatewayRefusal, match="binds once"):
        await harness.session.bind(project="inky")


async def test_a_native_session_may_list_but_not_bind(tmp_path: Path) -> None:
    harness = await a_harness(tmp_path)
    harness.session.native = True

    assert await harness.session.scry("projects") == [
        ProjectSummary(name="inky", description=None)
    ]
    with pytest.raises(GatewayRefusal, match="lives in your terminal"):
        await harness.session.bind(project="inky")


async def test_a_gateway_built_without_the_managers_is_a_wiring_bug() -> None:
    users, profile = await a_registered_profile()
    session = GatewaySession(
        channel_routes={}, current_agent_id="inkling", users=users, user_profile=profile
    )

    with pytest.raises(RuntimeError):
        await session.scry("projects")
    with pytest.raises(RuntimeError):
        await session.bind(project="inky")


async def test_inkling_hears_a_refusal_as_a_retry(tmp_path: Path) -> None:
    harness = await a_harness(tmp_path, registered=False)
    capability = GatewayCapability(session=harness.session)

    with pytest.raises(ModelRetry, match="no registered user"):
        await capability.scry(FAKE_CONTEXT, "projects")
    with pytest.raises(ModelRetry, match="no registered user"):
        await capability.bind(FAKE_CONTEXT, "inky")


async def test_an_mcp_runtime_reads_the_list_and_hears_a_refusal_as_a_tool_error(
    tmp_path: Path,
) -> None:
    harness = await a_harness(tmp_path)
    server = gateway_mcp(Depends(lambda: harness.session), FakeThreadManager())

    async with Client(server) as client:
        listed = await client.call_tool("scry", {"reveal": "projects"})
        with pytest.raises(ToolError, match=r"Available: inky\."):
            await client.call_tool("bind", {"project": "kraken"})

    assert listed.data == "- inky"


async def test_inkling_defers_the_bind_for_the_graph_to_resume(tmp_path: Path) -> None:
    # The bind is done before the deferral is raised: the thread is bound and the
    # workspace forked, and the run ends so the next one starts in it.
    harness = await a_harness(tmp_path)
    capability = GatewayCapability(session=harness.session)

    with pytest.raises(CallDeferred) as deferred:
        await capability.bind(FAKE_CONTEXT, "inky")

    assert deferred.value.metadata == {"kind": "bind", "project": "inky"}
    bound = await harness.threads.get(harness.thread.id)
    assert bound is not None
    assert bound.project_id is not None
