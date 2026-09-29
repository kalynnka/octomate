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
from uuid_utils.compat import uuid7

from octomate import Octomate
from octomate.capabilities.harness.deferred import DeferredSuspender
from octomate.capabilities.harness.events import ActionBatchEvent, MessageSentEvent
from octomate.capabilities.harness.react import ReactEventStream, ReactStreamEvent
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
from octomate.schemas.segments import FileData, FileSegment, TextSegment
from octomate.tentacles.channel import ChannelOutput
from tests.agent.test_command_manager import DiscoveringAgent


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
    return Octomate()


@pytest.fixture
def agent(app: Octomate) -> ExecutingAgent:
    return app.connect(ExecutingAgent())


@pytest.fixture
def context(agent: ExecutingAgent, tmp_path: Path) -> CommandContext:
    return CommandContext(
        agent_id=agent.id,
        user_id=uuid7(),
        address=ChannelAddress("im", "thread", "chat", "user", "thread"),
        cwd=tmp_path / "unprepared",
        conversation=Conversation(thread_id=uuid7(), agent_tentacle_id=agent.id),
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
