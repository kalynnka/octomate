"""Command dispatch validates live metadata and owns one turn through cleanup."""

import asyncio
from collections.abc import AsyncGenerator, Sequence
from contextlib import nullcontext
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Literal

import pytest
from pydantic_ai import AgentCapability
from pydantic_ai.tools import DeferredToolRequests
from sqlalchemy.ext.asyncio import AsyncEngine

from octomate import Octomate
from octomate.capabilities.harness.deferred import DeferredSuspender
from octomate.capabilities.harness.events import ActionBatchEvent, MessageSentEvent
from octomate.capabilities.harness.react import ReactEventStream, ReactStreamEvent
from octomate.database import async_session
from octomate.managers.gateway import OctomateSession
from octomate.schemas.commands import (
    CommandContext,
    CommandDescriptor,
    CommandError,
    CommandInvocation,
    CommandOutcome,
    CommandResult,
)
from octomate.schemas.conversation import ChannelAddress, Conversation
from octomate.schemas.events import MessageEvent
from octomate.schemas.segments import FileData, FileSegment, TextSegment
from octomate.schemas.user import User, UserProfile
from octomate.tentacles.channel import ChannelOutput
from octomate.types.permissions import PermissionMode
from tests.agent.test_command_manager import DiscoveringAgent
from tests.support.channels import FakeChannelTentacle
from tests.support.managers import a_project, a_registry


@dataclass
class ExecutingAgent(DiscoveringAgent):
    invocations: list[CommandInvocation] = field(default_factory=list)
    behavior: Literal["direct", "stream", "raise", "cancel"] = "direct"
    stream_closed: bool = False
    suspender: DeferredSuspender | None = None
    capabilities: Sequence[AgentCapability[None]] | None = None

    async def execute_command(
        self,
        context: CommandContext,
        invocation: CommandInvocation,
        *,
        deferred_suspender: DeferredSuspender | None = None,
        capabilities: Sequence[AgentCapability[None]] | None = None,
    ) -> CommandOutcome | ReactEventStream[ChannelOutput]:
        self.invocations.append(invocation)
        self.suspender = deferred_suspender
        self.capabilities = capabilities
        if self.behavior == "raise":
            raise RuntimeError("private runtime details")
        if self.behavior == "cancel":
            raise asyncio.CancelledError
        if self.behavior == "stream":
            return ReactEventStream(self.events())
        return CommandResult(segments=[TextSegment(data={"text": "Done"})])

    async def events(self) -> AsyncGenerator[ReactStreamEvent[ChannelOutput], None]:
        try:
            yield MessageSentEvent(segments=[TextSegment(data={"text": "Running"})])
            await asyncio.Event().wait()
        finally:
            self.stream_closed = True


class Suspender:
    async def suspend(self, requests: DeferredToolRequests) -> ActionBatchEvent | None:
        return None


@pytest.fixture
def app() -> Octomate:
    app = Octomate()
    app.connect(FakeChannelTentacle(octomate=app))
    return app


@pytest.fixture
def agent(app: Octomate) -> ExecutingAgent:
    return app.connect(ExecutingAgent())


@pytest.fixture
async def context(
    app: Octomate, agent: ExecutingAgent, in_memory_engine: AsyncEngine
) -> CommandContext:
    user = User(username="alice")
    async with async_session() as session:
        session.add(user)
        await session.commit()
    address = ChannelAddress("im", "thread", "chat", "user", "thread")
    thread = await app.threads.ensure(address)
    await app.threads.record_inbound(
        MessageEvent(
            tentacle_id=address.channel_tentacle_id,
            chat_type=address.chat_type,
            chat_id=address.chat_id,
            channel_thread_id=address.channel_thread_id,
            user_id=address.user_id,
            sender=UserProfile(channel_user_id=address.user_id, user_id=user.id),
        )
    )
    conversation = await app.conversations.ensure(thread.id, agent_tentacle_id=agent.id)
    await app.threads.record_handoff(
        thread, to_agent_tentacle_id=agent.id, to_model="test"
    )
    return CommandContext(
        agent_id=agent.id,
        user_id=user.id,
        address=address,
        cwd=app.workspaces.open(thread.id, None).path,
        conversation=conversation,
        model="test",
        permission_mode=agent.default_permission_mode,
    )


async def test_direct_execution_preserves_input_and_invalidates_catalogs(
    app: Octomate, agent: ExecutingAgent, context: CommandContext
) -> None:
    manager = app.commands
    agent.descriptors = {
        CommandDescriptor(
            id="skill", name="review", description="Review", accepts_attachments=True
        )
    }
    await manager.discover(agent, context)
    invocation = CommandInvocation(
        command_id="skill",
        arguments='  "raw argument"\n--flag=✓  ',
        attachments=[FileSegment(data=FileData(file="/resolved/input.txt"))],
    )
    async with manager.execute(agent, context, invocation) as result:
        assert isinstance(result, CommandResult)
        assert context.conversation is not None
        assert context.conversation.id in app.gateway.sessions
        async with manager.execute(agent, context, invocation) as busy:
            assert isinstance(busy, CommandError)
            assert busy.status == "busy"
    assert agent.invocations == [invocation]
    assert agent.invocations[0].arguments == invocation.arguments
    assert len(agent.calls) == 2
    assert not manager.catalogs
    assert not app.gateway.sessions
    assert not agent.turns
    assert not agent.streams
    assert context.cwd is not None
    assert not context.cwd.exists()


async def test_execution_uses_injected_managers_without_an_agent_host(
    app: Octomate, agent: ExecutingAgent, context: CommandContext
) -> None:
    agent.octomate = None
    async with app.commands.execute(
        agent, context, CommandInvocation(command_id="skill")
    ) as result:
        assert isinstance(result, CommandResult)
    assert len(agent.invocations) == 1
    assert not app.gateway.sessions


@pytest.mark.parametrize(
    "status", ["ready", "loading", "unsupported", "unavailable", "failed"]
)
async def test_current_catalog_state_controls_execution(
    app: Octomate,
    agent: ExecutingAgent,
    context: CommandContext,
    status: Literal["ready", "loading", "unsupported", "unavailable", "failed"],
) -> None:
    manager = app.commands
    await manager.discover(agent, context)
    # The runtime removes the selected command after it was shown to the user.
    agent.descriptors.clear()
    agent.status = status
    agent.failure = status == "failed"
    async with manager.execute(
        agent, context, CommandInvocation(command_id="skill")
    ) as result:
        assert isinstance(result, CommandError)
        assert result.status == {"ready": "unknown", "loading": "stale"}.get(
            status, status
        )
        assert "private runtime details" not in result.message
    assert not agent.invocations
    assert not app.gateway.sessions


@pytest.mark.parametrize("accepts", [None, False])
async def test_execution_requires_declared_attachment_support(
    app: Octomate, agent: ExecutingAgent, context: CommandContext, accepts: bool | None
) -> None:
    agent.descriptors = {
        CommandDescriptor(
            id="skill", name="review", description="Review", accepts_attachments=accepts
        )
    }
    invocation = CommandInvocation(
        command_id="skill",
        attachments=[FileSegment(data=FileData(file="/resolved/input.txt"))],
    )
    async with app.commands.execute(agent, context, invocation) as result:
        assert isinstance(result, CommandError)
        assert result.status == "unsupported"
    assert not agent.invocations


async def test_composer_execution_does_not_create_a_session(
    app: Octomate, agent: ExecutingAgent, context: CommandContext
) -> None:
    async with app.commands.execute(
        agent,
        replace(context, conversation=None),
        CommandInvocation(command_id="skill"),
    ) as result:
        assert isinstance(result, CommandError)
        assert result.status == "unavailable"
    assert not agent.calls
    assert not agent.invocations
    assert not app.gateway.sessions


async def test_active_chat_turn_prevents_command_execution(
    app: Octomate, agent: ExecutingAgent, context: CommandContext
) -> None:
    assert context.conversation is not None
    session = OctomateSession(
        channel_routes={},
        current_agent_id=agent.id,
        conversation_id=context.conversation.id,
    )
    async with app.gateway.driving(session):
        async with app.commands.execute(
            agent, context, CommandInvocation(command_id="skill")
        ) as result:
            assert isinstance(result, CommandError)
            assert result.status == "busy"
        assert app.gateway.get(context.conversation.id) is session
    assert not agent.calls
    assert not agent.invocations


@pytest.mark.parametrize("behavior", ["raise", "cancel"])
async def test_backend_failure_releases_guard_and_invalidates_catalog(
    app: Octomate,
    agent: ExecutingAgent,
    context: CommandContext,
    behavior: Literal["raise", "cancel"],
) -> None:
    agent.behavior = behavior
    if behavior == "cancel":
        with pytest.raises(asyncio.CancelledError):
            async with app.commands.execute(
                agent, context, CommandInvocation(command_id="skill")
            ):
                pytest.fail("cancellation was swallowed")
    else:
        async with app.commands.execute(
            agent, context, CommandInvocation(command_id="skill")
        ) as result:
            assert isinstance(result, CommandError)
            assert result.status == "failed"
            assert "private runtime details" not in result.message
    assert not app.gateway.sessions
    assert not app.commands.catalogs
    assert len(agent.invocations) == 1


@pytest.mark.parametrize("cancel", [False, True])
async def test_stream_owns_guard_until_cleanup_and_forwards_run_hooks(
    app: Octomate, agent: ExecutingAgent, context: CommandContext, cancel: bool
) -> None:
    agent.behavior = "stream"
    assert context.conversation is not None
    session = OctomateSession(
        channel_routes={},
        current_agent_id=agent.id,
        conversation_id=context.conversation.id,
    )
    suspender = Suspender()
    capabilities: list[AgentCapability[None]] = []
    with pytest.raises(asyncio.CancelledError) if cancel else nullcontext():
        async with app.commands.execute(
            agent,
            context,
            CommandInvocation(command_id="skill"),
            session=session,
            deferred_suspender=suspender,
            capabilities=capabilities,
        ) as events:
            assert not isinstance(events, CommandResult | CommandError)
            assert isinstance(await anext(events), MessageSentEvent)
            assert not agent.stream_closed
            assert app.gateway.get(context.conversation.id) is session
            other = OctomateSession(
                channel_routes={},
                current_agent_id=agent.id,
                conversation_id=context.conversation.id,
            )
            with pytest.raises(RuntimeError, match="already has a turn"):
                app.gateway.register(other)
            if cancel:
                raise asyncio.CancelledError
    assert agent.suspender is suspender
    assert agent.capabilities is capabilities
    assert agent.stream_closed
    assert not app.gateway.sessions
    assert not app.commands.catalogs


@pytest.mark.parametrize("during_probe", [False, True])
@pytest.mark.parametrize(
    "change",
    ["profile", "channel", "route", "session", "workspace", "model", "permission"],
)
async def test_execution_checks_access_and_selection_after_discovery(
    app: Octomate,
    agent: ExecutingAgent,
    context: CommandContext,
    tmp_path: Path,
    change: str,
    during_probe: bool,
) -> None:
    assert context.conversation is not None

    async def invoke() -> CommandOutcome:
        async with app.commands.execute(
            agent, context, CommandInvocation(command_id="skill")
        ) as result:
            assert isinstance(result, CommandResult | CommandError)
            return result

    task = None
    if during_probe:
        agent.release = asyncio.Event()
        task = asyncio.create_task(invoke())
        await agent.entered.wait()
    if change == "profile":
        profile = await app.users.profile("im", context.address.user_id)
        assert profile is not None
        async with async_session() as session:
            user = await session.get(User, context.user_id)
        assert user is not None
        await app.users.unlink_profile(user, profile.id)
    elif change == "channel":
        app.channels["im"].config.agents = ["other"]
    elif change == "route":
        await app.threads.record_handoff(context.address, to_agent_tentacle_id="other")
    elif change == "model":
        await app.threads.record_handoff(
            context.address, to_agent_tentacle_id=agent.id, to_model="opus"
        )
    elif change == "permission":
        agent.permission_modes = (PermissionMode(value="plan", name="Plan"),)
        await app.conversations.set_permission_mode(
            context.conversation.model_copy(), "plan"
        )
    elif change == "workspace":
        project = a_project(tmp_path / "project")
        app.workspaces.projects = await a_registry(project)
        await app.threads.bind(context.conversation.thread_id, project)
    else:
        async with async_session() as session:
            conversation = await session.get(Conversation, context.conversation.id)
            assert conversation is not None
            conversation.external_id = "replacement-session"
            await session.commit()
    if task is not None:
        assert agent.release is not None
        agent.release.set()
        result = await task
    else:
        result = await invoke()
    assert isinstance(result, CommandError)
    assert result.status == (
        "unavailable" if change in {"profile", "channel"} else "stale"
    )
    assert not agent.invocations
    assert len(agent.calls) == 1
    assert not app.gateway.sessions


@pytest.mark.parametrize("selection", ["user", "address", "missing", "subagent"])
async def test_execution_rejects_foreign_or_unavailable_conversations(
    app: Octomate, agent: ExecutingAgent, context: CommandContext, selection: str
) -> None:
    assert context.conversation is not None
    if selection == "user":
        other = User(username="bob")
        async with async_session() as session:
            session.add(other)
            await session.commit()
        await app.users.ensure_profile(
            "im", UserProfile(channel_user_id="bob", user_id=other.id)
        )
        context = replace(
            context, user_id=other.id, address=replace(context.address, user_id="bob")
        )
    elif selection == "address":
        context = replace(
            context, address=replace(context.address, channel_thread_id="other")
        )
    elif selection == "missing":
        context = replace(
            context,
            conversation=Conversation(
                thread_id=context.conversation.thread_id, agent_tentacle_id=agent.id
            ),
        )
    else:
        child = await app.conversations.ensure(
            context.conversation.thread_id,
            agent_tentacle_id=agent.id,
            subagent_id="child",
            parent_conversation_id=context.conversation.id,
        )
        context = replace(context, conversation=child)
    async with app.commands.execute(
        agent, context, CommandInvocation(command_id="skill")
    ) as result:
        assert isinstance(result, CommandError)
        assert result.status == ("stale" if selection == "subagent" else "unavailable")
    assert len(agent.calls) == 1
    assert not agent.invocations
