"""Command dispatch validates live metadata and owns one turn through cleanup."""

import asyncio
from collections.abc import AsyncGenerator, Sequence
from contextlib import AsyncExitStack, aclosing, nullcontext
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Literal
from unittest.mock import AsyncMock

import pytest
from pydantic_ai import AgentCapability
from sqlalchemy.ext.asyncio import AsyncEngine

from octomate import Octomate
from octomate.capabilities.harness.deferred import DeferredSuspender
from octomate.capabilities.harness.events import MessageSentEvent
from octomate.capabilities.harness.react import ReactStreamEvent
from octomate.database import async_session
from octomate.managers.commands import CommandManager, ValidatedCommand
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
from octomate.schemas.thread import Thread, ThreadCommand, ThreadMessage
from octomate.schemas.user import User, UserProfile
from octomate.tentacles.channel import ChannelOutput
from octomate.types.permissions import PermissionMode
from tests.agent.test_command_manager import DiscoveringAgent
from tests.support.channels import FakeChannelTentacle
from tests.support.managers import RecordingSuspender, a_project, a_registry


@dataclass
class ExecutingAgent(DiscoveringAgent):
    invocations: list[CommandInvocation] = field(default_factory=list)
    receipts: list[ThreadCommand] = field(default_factory=list)
    behavior: Literal[
        "direct",
        "direct_cleanup_error",
        "empty",
        "stream",
        "stream_complete",
        "stream_raise",
        "stream_cleanup_error",
        "raise",
        "cancel",
    ] = "direct"
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
    ) -> AsyncGenerator[CommandOutcome | ReactStreamEvent[ChannelOutput], None]:
        async with async_session() as session:
            self.receipts = list(await session.list(ThreadCommand, limit=None))
        self.invocations.append(invocation)
        self.suspender = deferred_suspender
        self.capabilities = capabilities
        if self.behavior == "raise":
            raise RuntimeError("private runtime details")
        if self.behavior == "cancel":
            raise asyncio.CancelledError
        if self.behavior == "empty":
            return
        if self.behavior.startswith("stream"):
            async with aclosing(self.events()) as events:
                async for event in events:
                    yield event
            return
        try:
            yield CommandResult(segments=[TextSegment(data={"text": "Done"})])
        finally:
            self.stream_closed = True
            if self.behavior == "direct_cleanup_error":
                raise RuntimeError("private cleanup details")

    async def events(self) -> AsyncGenerator[ReactStreamEvent[ChannelOutput], None]:
        try:
            yield MessageSentEvent(segments=[TextSegment(data={"text": "Running"})])
            if self.behavior == "stream_complete":
                return
            if self.behavior == "stream_raise":
                raise RuntimeError("private stream details")
            await asyncio.Event().wait()
        finally:
            self.stream_closed = True
            if self.behavior == "stream_cleanup_error":
                raise RuntimeError("private cleanup details")


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
    async with AsyncExitStack() as stack:
        validated = await stack.enter_async_context(
            manager.validate(agent, context, invocation, delivery_id="command-1")
        )
        assert isinstance(validated, ValidatedCommand)
        result = await stack.enter_async_context(
            manager.execute(
                agent, context, invocation, validated, delivery_id="command-1"
            )
        )
        assert isinstance(result, CommandResult)
        assert context.conversation is not None
        assert context.conversation.id in app.gateway.sessions
        async with manager.validate(
            agent, context, invocation, delivery_id="command-1"
        ) as busy:
            assert isinstance(busy, CommandError)
            assert busy.status == "busy"
    assert agent.invocations == [invocation]
    assert agent.invocations[0].arguments == invocation.arguments
    assert len(agent.receipts) == 1
    assert agent.receipts[0].invocation == invocation
    assert agent.receipts[0].outcome is None
    assert agent.receipts[0].conversation_id == context.conversation.id
    recorded = await app.threads.find_message(
        agent.receipts[0].thread_id, "command-1", "inbound"
    )
    assert isinstance(recorded, ThreadCommand)
    assert recorded.outcome == result
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
    invocation = CommandInvocation(command_id="skill")
    async with AsyncExitStack() as stack:
        validated = await stack.enter_async_context(
            app.commands.validate(agent, context, invocation, delivery_id="command-1")
        )
        assert isinstance(validated, ValidatedCommand)
        result = await stack.enter_async_context(
            app.commands.execute(
                agent, context, invocation, validated, delivery_id="command-1"
            )
        )
        assert isinstance(result, CommandResult)
    assert len(agent.invocations) == 1
    assert not app.gateway.sessions


async def test_validation_and_execution_share_guard_without_repeating_validation(
    app: Octomate,
    agent: ExecutingAgent,
    context: CommandContext,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    invocation = CommandInvocation(command_id="skill")
    profile_lookup = AsyncMock(wraps=app.users.profile)
    monkeypatch.setattr(app.users, "profile", profile_lookup)
    async with app.commands.validate(
        agent, context, invocation, delivery_id="command-1"
    ) as validated:
        assert isinstance(validated, ValidatedCommand)
        assert context.conversation is not None
        assert validated.surface is not None
        assert validated.profile is not None
        assert validated.surface.id == context.conversation.thread_id
        assert validated.profile.user_id == context.user_id
        assert validated.descriptor.id == invocation.command_id
        assert context.conversation.id in app.gateway.sessions
        assert not agent.invocations
        async with async_session() as session:
            assert await session.count(ThreadCommand) == 0
        async with app.commands.validate(
            agent, context, invocation, delivery_id="command-2"
        ) as busy:
            assert isinstance(busy, CommandError)
            assert busy.status == "busy"
        async with app.commands.execute(
            agent, context, invocation, validated, delivery_id="command-1"
        ) as output:
            assert isinstance(output, CommandResult)
        assert context.conversation.id in app.gateway.sessions
    profile_lookup.assert_awaited_once()
    assert len(agent.calls) == 1
    assert agent.invocations == [invocation]
    assert not app.gateway.sessions


@pytest.mark.parametrize("behavior", ["direct", "direct_cleanup_error", "empty"])
async def test_direct_outcome_requires_a_result_and_successful_cleanup(
    app: Octomate,
    agent: ExecutingAgent,
    context: CommandContext,
    behavior: Literal["direct", "direct_cleanup_error", "empty"],
) -> None:
    agent.behavior = behavior
    invocation = CommandInvocation(command_id="skill")
    async with AsyncExitStack() as stack:
        validated = await stack.enter_async_context(
            app.commands.validate(agent, context, invocation, delivery_id="command-1")
        )
        assert isinstance(validated, ValidatedCommand)
        result = await stack.enter_async_context(
            app.commands.execute(
                agent, context, invocation, validated, delivery_id="command-1"
            )
        )
        assert isinstance(result, CommandResult | CommandError)
        assert result.status == ("completed" if behavior == "direct" else "failed")
        assert agent.stream_closed is (behavior != "empty")
    assert not app.gateway.sessions
    async with app.commands.validate(
        agent, context, invocation, delivery_id="command-1"
    ) as repeated:
        assert repeated == result
    assert len(agent.invocations) == 1


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
    async with manager.validate(
        agent, context, CommandInvocation(command_id="skill"), delivery_id="command-1"
    ) as result:
        assert isinstance(result, CommandError)
        assert result.status == {"ready": "unknown", "loading": "stale"}.get(
            status, status
        )
        assert "private runtime details" not in result.message
    assert not agent.invocations
    assert not app.gateway.sessions

    async with async_session() as session:
        assert await session.count(ThreadCommand) == 0


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
    async with app.commands.validate(
        agent, context, invocation, delivery_id="command-1"
    ) as result:
        assert isinstance(result, CommandError)
        assert result.status == "unsupported"
    assert not agent.invocations


async def test_composer_execution_does_not_create_a_session(
    app: Octomate, agent: ExecutingAgent, context: CommandContext
) -> None:
    async with app.commands.validate(
        agent,
        replace(context, conversation=None),
        CommandInvocation(command_id="skill"),
        delivery_id="command-1",
    ) as result:
        assert isinstance(result, CommandError)
        assert result.status == "unavailable"
    assert len(agent.calls) == 1
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
        async with app.commands.validate(
            agent,
            context,
            CommandInvocation(command_id="skill"),
            delivery_id="command-1",
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
    invocation = CommandInvocation(command_id="skill")
    async with AsyncExitStack() as stack:
        validated = await stack.enter_async_context(
            app.commands.validate(agent, context, invocation, delivery_id="command-1")
        )
        assert isinstance(validated, ValidatedCommand)
        stack.enter_context(
            pytest.raises(asyncio.CancelledError)
            if behavior == "cancel"
            else nullcontext()
        )
        result = await stack.enter_async_context(
            app.commands.execute(
                agent, context, invocation, validated, delivery_id="command-1"
            )
        )
        assert isinstance(result, CommandError)
        assert result.status == "failed"
        assert "private runtime details" not in result.message
    assert not app.gateway.sessions
    assert not app.commands.catalogs
    assert len(agent.invocations) == 1

    async with app.commands.validate(
        agent, context, CommandInvocation(command_id="skill"), delivery_id="command-1"
    ) as repeated:
        assert isinstance(repeated, CommandError)
        assert repeated.status == "failed"
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
    suspender = RecordingSuspender()
    capabilities: list[AgentCapability[None]] = []
    with pytest.raises(asyncio.CancelledError) if cancel else nullcontext():
        invocation = CommandInvocation(command_id="skill")
        async with AsyncExitStack() as stack:
            validated = await stack.enter_async_context(
                app.commands.validate(
                    agent, context, invocation, session=session, delivery_id="command-1"
                )
            )
            assert isinstance(validated, ValidatedCommand)
            events = await stack.enter_async_context(
                app.commands.execute(
                    agent,
                    context,
                    invocation,
                    validated,
                    deferred_suspender=suspender,
                    capabilities=capabilities,
                    delivery_id="command-1",
                )
            )
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

    async with app.commands.validate(
        agent, context, CommandInvocation(command_id="skill"), delivery_id="command-1"
    ) as repeated:
        assert isinstance(repeated, CommandError)
        assert repeated.status == "failed"
    assert len(agent.invocations) == 1


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
        async with app.commands.validate(
            agent,
            context,
            CommandInvocation(command_id="skill"),
            delivery_id="command-1",
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


@pytest.mark.parametrize(
    "selection", ["user", "address", "other_surface", "missing", "subagent"]
)
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
    elif selection == "other_surface":
        address = replace(context.address, channel_thread_id="other")
        await app.threads.record_inbound(
            MessageEvent(
                tentacle_id=address.channel_tentacle_id,
                chat_id=address.chat_id,
                chat_type=address.chat_type,
                channel_thread_id=address.channel_thread_id,
                user_id=address.user_id,
                sender=UserProfile(
                    channel_user_id=address.user_id, user_id=context.user_id
                ),
            )
        )
        context = replace(context, address=address)
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
    async with app.commands.validate(
        agent, context, CommandInvocation(command_id="skill"), delivery_id="command-1"
    ) as result:
        assert isinstance(result, CommandError)
        assert result.status == ("stale" if selection == "subagent" else "unavailable")
    assert len(agent.calls) == 1
    assert not agent.invocations


@pytest.mark.parametrize("missing_outcome", [False, True])
async def test_delivery_replay_uses_the_ledger_after_manager_recreation(
    app: Octomate, agent: ExecutingAgent, context: CommandContext, missing_outcome: bool
) -> None:
    invocation = CommandInvocation(command_id="skill")
    async with AsyncExitStack() as stack:
        validated = await stack.enter_async_context(
            app.commands.validate(agent, context, invocation, delivery_id="command-1")
        )
        assert isinstance(validated, ValidatedCommand)
        original = await stack.enter_async_context(
            app.commands.execute(
                agent, context, invocation, validated, delivery_id="command-1"
            )
        )
        assert isinstance(original, CommandResult)
    if missing_outcome:
        async with async_session() as session:
            receipt = await session.get(ThreadCommand, agent.receipts[0].id)
            assert receipt is not None
            receipt.outcome = None
            await session.commit()
    manager = CommandManager(
        tentacles=app.tentacles,
        users=app.users,
        conversations=app.conversations,
        threads=app.threads,
        workspaces=app.workspaces,
        gateway=app.gateway,
    )
    agent.failure = True
    async with manager.validate(
        agent, context, invocation, delivery_id="command-1"
    ) as repeated:
        if missing_outcome:
            assert isinstance(repeated, CommandError)
            assert "already accepted" in repeated.message
        else:
            assert repeated == original
    assert len(agent.invocations) == 1
    async with async_session() as session:
        assert await session.count(ThreadCommand) == 1


async def test_concurrent_duplicate_is_busy_and_a_new_delivery_can_run(
    app: Octomate, agent: ExecutingAgent, context: CommandContext
) -> None:
    agent.release = asyncio.Event()
    invocation = CommandInvocation(command_id="skill")

    async def invoke(delivery_id: str) -> CommandOutcome:
        async with AsyncExitStack() as stack:
            validated = await stack.enter_async_context(
                app.commands.validate(
                    agent, context, invocation, delivery_id=delivery_id
                )
            )
            if isinstance(validated, ValidatedCommand):
                result = await stack.enter_async_context(
                    app.commands.execute(
                        agent, context, invocation, validated, delivery_id=delivery_id
                    )
                )
            else:
                result = validated
            assert isinstance(result, CommandResult | CommandError)
            return result

    async with asyncio.TaskGroup() as tasks:
        first = tasks.create_task(invoke("command-1"))
        await agent.entered.wait()
        busy = await invoke("command-1")
        assert busy.status == "busy"
        agent.release.set()
    assert (await invoke("command-1")) == first.result()
    assert len(agent.invocations) == 1
    assert (await invoke("command-2")).status == "completed"
    assert len(agent.invocations) == 2
    async with async_session() as session:
        assert await session.count(ThreadCommand) == 2


@pytest.mark.parametrize("conflict", ["arguments", "sender", "conversation"])
async def test_delivery_id_cannot_be_reused_for_another_request(
    app: Octomate, agent: ExecutingAgent, context: CommandContext, conflict: str
) -> None:
    invocation = CommandInvocation(command_id="skill")
    async with AsyncExitStack() as stack:
        validated = await stack.enter_async_context(
            app.commands.validate(agent, context, invocation, delivery_id="command-1")
        )
        assert isinstance(validated, ValidatedCommand)
        await stack.enter_async_context(
            app.commands.execute(
                agent, context, invocation, validated, delivery_id="command-1"
            )
        )
    other = await app.users.ensure_profile("im", UserProfile(channel_user_id="bob"))
    async with async_session() as session:
        receipt = await session.get(ThreadCommand, agent.receipts[0].id)
        assert receipt is not None
        if conflict == "sender":
            receipt.sender_id = other.id
        if conflict == "conversation":
            receipt.conversation_id = None
        await session.commit()
    if conflict == "arguments":
        invocation.arguments = "a different action"
    async with app.commands.validate(
        agent, context, invocation, delivery_id="command-1"
    ) as repeated:
        assert isinstance(repeated, CommandError)
        assert "another request" in repeated.message
    assert len(agent.invocations) == 1


async def test_replay_still_requires_current_access(
    app: Octomate, agent: ExecutingAgent, context: CommandContext
) -> None:
    invocation = CommandInvocation(command_id="skill")
    async with AsyncExitStack() as stack:
        validated = await stack.enter_async_context(
            app.commands.validate(agent, context, invocation, delivery_id="command-1")
        )
        assert isinstance(validated, ValidatedCommand)
        await stack.enter_async_context(
            app.commands.execute(
                agent, context, invocation, validated, delivery_id="command-1"
            )
        )
    async with async_session() as session:
        user = await session.get(User, context.user_id)
    assert user is not None
    await app.users.unlink_profile(user, agent.receipts[0].sender_id)
    async with app.commands.validate(
        agent, context, invocation, delivery_id="command-1"
    ) as repeated:
        assert isinstance(repeated, CommandError)
        assert repeated.status == "unavailable"
    assert len(agent.invocations) == 1


@pytest.mark.parametrize("stage", ["receipt", "outcome"])
async def test_persistence_failure_never_repeats_runtime_effects(
    app: Octomate,
    agent: ExecutingAgent,
    context: CommandContext,
    monkeypatch: pytest.MonkeyPatch,
    stage: str,
) -> None:
    invocation = CommandInvocation(command_id="skill")
    with monkeypatch.context() as patch:
        patch.setattr(
            app.threads,
            "store_message" if stage == "receipt" else "record_command_outcome",
            AsyncMock(side_effect=RuntimeError("storage unavailable")),
        )
        async with AsyncExitStack() as stack:
            validated = await stack.enter_async_context(
                app.commands.validate(
                    agent, context, invocation, delivery_id="command-1"
                )
            )
            assert isinstance(validated, ValidatedCommand)
            with pytest.raises(RuntimeError, match="storage"):
                await stack.enter_async_context(
                    app.commands.execute(
                        agent, context, invocation, validated, delivery_id="command-1"
                    )
                )
    assert not app.gateway.sessions
    assert len(agent.invocations) == (0 if stage == "receipt" else 1)
    if stage == "outcome":
        async with app.commands.validate(
            agent, context, invocation, delivery_id="command-1"
        ) as repeated:
            assert isinstance(repeated, CommandError)
            assert "already accepted" in repeated.message
        assert len(agent.invocations) == 1


@pytest.mark.parametrize(
    "behavior", ["stream_complete", "stream_raise", "stream_cleanup_error"]
)
async def test_stream_outcome_is_recorded_after_consumption_and_cleanup(
    app: Octomate,
    agent: ExecutingAgent,
    context: CommandContext,
    behavior: Literal["stream_complete", "stream_raise", "stream_cleanup_error"],
) -> None:
    agent.behavior = behavior
    invocation = CommandInvocation(command_id="skill")
    with (
        nullcontext() if behavior == "stream_complete" else pytest.raises(RuntimeError)
    ):
        async with AsyncExitStack() as stack:
            validated = await stack.enter_async_context(
                app.commands.validate(
                    agent, context, invocation, delivery_id="command-1"
                )
            )
            assert isinstance(validated, ValidatedCommand)
            events = await stack.enter_async_context(
                app.commands.execute(
                    agent, context, invocation, validated, delivery_id="command-1"
                )
            )
            assert not isinstance(events, CommandResult | CommandError)
            if behavior == "stream_cleanup_error":
                await anext(events)
            else:
                assert len([event async for event in events]) == 1
    assert agent.stream_closed
    assert not app.gateway.sessions
    assert not app.commands.catalogs
    async with app.commands.validate(
        agent, context, invocation, delivery_id="command-1"
    ) as repeated:
        assert isinstance(repeated, CommandResult | CommandError)
        assert repeated.status == (
            "completed" if behavior == "stream_complete" else "failed"
        )
        if isinstance(repeated, CommandError):
            assert "private" not in repeated.message
    assert len(agent.invocations) == 1


@pytest.mark.parametrize("behavior", ["direct", "stream_complete"])
async def test_recording_outcomes_does_not_mutate_the_callers_receipt(
    app: Octomate,
    agent: ExecutingAgent,
    context: CommandContext,
    monkeypatch: pytest.MonkeyPatch,
    behavior: Literal["direct", "stream_complete"],
) -> None:
    agent.behavior = behavior
    receipts: list[ThreadCommand] = []
    store = app.threads.store_message

    async def capture(message: ThreadMessage, thread: Thread) -> None:
        assert isinstance(message, ThreadCommand)
        receipts.append(message)
        await store(message, thread)

    monkeypatch.setattr(app.threads, "store_message", capture)
    invocation = CommandInvocation(command_id="skill")
    async with AsyncExitStack() as stack:
        validated = await stack.enter_async_context(
            app.commands.validate(agent, context, invocation, delivery_id="command-1")
        )
        assert isinstance(validated, ValidatedCommand)
        result = await stack.enter_async_context(
            app.commands.execute(
                agent, context, invocation, validated, delivery_id="command-1"
            )
        )
        if not isinstance(result, CommandResult | CommandError):
            assert len([event async for event in result]) == 1
    assert len(receipts) == 1
    assert receipts[0].outcome is None
    async with async_session() as session:
        stored = await session.get(ThreadCommand, receipts[0].id)
        assert stored is not None
        assert isinstance(stored.outcome, CommandResult)
