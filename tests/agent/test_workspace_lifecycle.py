"""Core lifecycle operations select the requesting user's volume."""

import asyncio
from collections.abc import AsyncIterator
from dataclasses import replace
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from pydantic_ai import AgentRunResult
from pydantic_graph import End, GraphRunContext
from sqlalchemy.ext.asyncio import AsyncEngine

from octomate import Octomate
from octomate.database import async_session
from octomate.managers.workspaces.base import UnregisteredProjectError
from octomate.reflex.nodes.awake import Awake
from octomate.reflex.nodes.react import React
from octomate.reflex.nodes.teleport import Teleport
from octomate.reflex.state import ReflexDeps, ReflexResult, ReflexState, ResponseTarget
from octomate.reflex.suspender import TeleportRequest
from octomate.schemas.awakes import SummonSignal
from octomate.schemas.conversation import ChannelAddress
from octomate.schemas.triage import AgentRouteKey, SummonDecision
from octomate.schemas.user import UserProfile
from octomate.schemas.volumes import TreeVolume
from octomate.tentacles.channel import ChannelOutput
from tests.support.agents import FakeAgent
from tests.support.channels import FakeChannelTentacle
from tests.support.managers import a_project, a_registry
from tests.support.users import a_user


@pytest.fixture
async def context(
    in_memory_engine: AsyncEngine, tmp_path: Path
) -> AsyncIterator[GraphRunContext[ReflexState, ReflexDeps]]:
    app = Octomate()
    app.connect(FakeAgent())
    app.connect(FakeChannelTentacle(octomate=app))
    owner = await a_user("alice")
    root = tmp_path / "project"
    root.mkdir()
    (root / "work.txt").write_text("Original")
    app.workspaces.projects = await a_registry(a_project(root, name="demo"))
    address = ChannelAddress("im", "thread", "shared", "alice", "topic", shared=True)
    target = ResponseTarget(channel_id="im", address=address)
    state = ReflexState(
        thread=await app.threads.ensure(address),
        user_profile=UserProfile(
            channel_tentacle_id="im", channel_user_id="alice", user_id=owner.id
        ),
        source_target=target,
        target=target,
        selection=AgentRouteKey("inkling", "test"),
        decision=SummonDecision(
            agent_id="inkling", model="test", reason="Work", hint="", brief=""
        ),
    )
    async with app.nerve.publish(address) as impulses:
        yield GraphRunContext(
            state=state,
            deps=ReflexDeps(
                channels=app.channels,
                agents=app.agents,
                thread_manager=app.threads,
                conversation_manager=app.conversations,
                action_manager=app.deferred_actions,
                gateway=app.gateway,
                workspaces=app.workspaces,
                impulses=impulses,
            ),
        )


@pytest.mark.parametrize("entry", ["summon", "teleport"])
@pytest.mark.parametrize("registered", [False, True])
async def test_project_preparation_uses_the_sender_and_starting_ref(
    context: GraphRunContext[ReflexState, ReflexDeps], entry: str, registered: bool
) -> None:
    state = context.state
    assert state.target is not None
    assert state.target.address is not None
    assert state.user_profile is not None
    assert state.decision is not None
    assert state.thread is not None
    if not registered:
        state.user_profile = UserProfile(channel_user_id="visitor")
    if entry == "summon":
        node = Awake(
            SummonSignal(
                decision=state.decision,
                destination=state.target.address,
                agent_id="inkling",
                user_profile=state.user_profile,
                project="demo",
                ref="HEAD",
            )
        )
    else:
        node = Teleport(
            request=TeleportRequest(
                hint="Move",
                tool_call_id=None,
                destination=state.target.address,
                new_thread=False,
                project="demo",
                ref="HEAD",
            ),
            origin=state.target,
            agent_id="inkling",
        )
    if not registered:
        with pytest.raises(UnregisteredProjectError):
            await node.run(context)
        thread = await context.deps.thread_manager.get(state.thread.id)
        assert thread is not None
        assert thread.project_id is None
        async with async_session() as session:
            assert await session.count(TreeVolume) == 0
        return
    assert isinstance(await node.run(context), React)
    async with async_session() as session:
        [volume] = await session.list(TreeVolume)
    assert volume.owner_id == state.user_profile.user_id
    assert volume.thread_id == state.thread.id
    assert volume.project_id == state.thread.project_id
    assert volume.base_ref == "HEAD"
    assert (context.deps.workspaces.path(volume) / "work.txt").read_text() == "Original"


@pytest.mark.parametrize("registered", [False, True])
@pytest.mark.parametrize("outcome", ["completed", "failed", "cancelled"])
async def test_react_saves_only_the_registered_senders_tree(
    context: GraphRunContext[ReflexState, ReflexDeps],
    registered: bool,
    outcome: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert context.state.target is not None
    if not registered:
        context.state.user_profile = UserProfile(channel_user_id="visitor")
    error = (
        asyncio.CancelledError()
        if outcome == "cancelled"
        else RuntimeError("run failed")
        if outcome == "failed"
        else None
    )
    react = React()
    monkeypatch.setattr(
        react,
        "react",
        AsyncMock(
            return_value=End(ReflexResult(decision=None, target=context.state.target)),
            side_effect=error,
        ),
    )
    save = AsyncMock()
    monkeypatch.setattr(context.deps.workspaces, "save", save)
    if error is not None:
        with pytest.raises(type(error)):
            await react.run(context)
    else:
        await react.run(context)
    if registered and outcome != "cancelled":
        assert context.state.user_profile is not None
        save.assert_awaited_once_with(
            context.state.thread, context.state.user_profile.user_id
        )
    else:
        save.assert_not_awaited()


@pytest.mark.parametrize("operation", ["dismiss", "teleport"])
async def test_workspace_operations_leave_the_other_senders_tree_untouched(
    context: GraphRunContext[ReflexState, ReflexDeps],
    operation: str,
) -> None:
    state, deps = context.state, context.deps
    assert state.thread is not None
    assert state.user_profile is not None
    assert state.user_profile.user_id is not None
    project = deps.workspaces.projects.get("demo")
    assert project is not None
    state.thread = await deps.thread_manager.bind(state.thread.id, project)
    own = await deps.workspaces.open(
        state.thread.id, project, state.user_profile.user_id
    )
    other_user = await a_user("bob")
    other = await deps.workspaces.open(state.thread.id, project, other_user.id)
    await own.prepare()
    await other.prepare()
    (own.path / "work.txt").write_text("Alice's unfinished work")
    (other.path / "work.txt").write_text("Bob's unfinished work")
    if operation == "dismiss":
        session, suspender, _ = await deps.runtime.resources(context)
        assert session is not None
        session.dismissing = True
        await deps.runtime.finish(
            context, session, suspender, AgentRunResult[ChannelOutput]("Done")
        )
        assert not own.path.exists()
    else:
        assert state.target is not None
        assert state.target.address is not None
        node = Teleport(
            request=TeleportRequest(
                hint="Move",
                tool_call_id=None,
                destination=replace(
                    state.target.address, chat_type="group", channel_thread_id=None
                ),
            ),
            origin=state.target,
            agent_id="inkling",
        )
        assert isinstance(await node.run(context), End)
        assert state.thread.id != own.thread_id
        carried = await deps.workspaces.find(
            state.user_profile.user_id, state.thread.id, project.id
        )
        assert carried is not None
        assert (
            deps.workspaces.path(carried) / "work.txt"
        ).read_text() == "Alice's unfinished work"
        assert (
            await deps.workspaces.find(other_user.id, state.thread.id, project.id)
            is None
        )
    assert (other.path / "work.txt").read_text() == "Bob's unfinished work"
    assert (
        await deps.workspaces.find(
            state.user_profile.user_id, own.thread_id, project.id
        )
        is not None
    )
