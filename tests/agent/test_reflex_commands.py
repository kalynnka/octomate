"""Explicit command signals use Reflex delivery without starting another agent run."""

import asyncio
from collections.abc import AsyncGenerator
from dataclasses import replace
from unittest.mock import AsyncMock

import anyio
import pytest
from pydantic_ai import AgentRunResult, AgentRunResultEvent
from pydantic_ai.messages import (
    ModelRequest,
    ModelResponse,
    TextPart,
    ToolCallPart,
    UserPromptPart,
)
from pydantic_ai.tools import DeferredToolRequests
from sqlalchemy.ext.asyncio import AsyncEngine

from octomate import Octomate
from octomate.capabilities.gateway import GatewayCapability
from octomate.capabilities.harness.events import MessageSentEvent
from octomate.capabilities.harness.react import ReactStreamEvent
from octomate.config import ChannelConfig, ChannelStreamConfig
from octomate.config.channels import TrunklineChannelConfig
from octomate.database import async_session
from octomate.reflex.graph import reflex_graph
from octomate.reflex.nodes.awake import Awake
from octomate.reflex.state import ReflexDeps, ReflexState
from octomate.reflex.suspender import ReflexSuspender
from octomate.schemas.awakes import CommandSignal, DeferredActionBatchResponse
from octomate.schemas.commands import (
    CommandContext,
    CommandError,
    CommandInvocation,
    CommandResult,
)
from octomate.schemas.conversation import ChannelAddress
from octomate.schemas.events import MessageEvent
from octomate.schemas.segments import TextSegment
from octomate.schemas.thread import ThreadCommand
from octomate.schemas.triage import (
    TELEPORT_DEFER_KIND,
    AgentRouteKey,
    HereLanding,
    SummonDecision,
)
from octomate.schemas.user import UserProfile
from octomate.tentacles.channel import ChannelOutput
from octomate.tentacles.trunkline.base import (
    TrunklineStreamItem,
    TrunklineTentacle,
    current_sink,
)
from tests.agent.test_command_execution import ExecutingAgent
from tests.support.agents import FakeAgent
from tests.support.channels import FakeChannelTentacle
from tests.support.managers import RecordingWorkspaceManager
from tests.support.users import a_user


@pytest.fixture
async def scenario(
    in_memory_engine: AsyncEngine,
) -> tuple[Octomate, ExecutingAgent, FakeChannelTentacle, CommandSignal]:
    app = Octomate(workspaces=RecordingWorkspaceManager())
    agent = app.connect(ExecutingAgent())
    agent.commands = app.commands
    channel = app.connect(
        FakeChannelTentacle(
            octomate=app, config=ChannelConfig(type="fake", agents=[agent.id])
        )
    )
    user = await a_user()
    address = ChannelAddress("im", "thread", "chat", "user", "thread")
    thread = await app.threads.ensure(address)
    await app.threads.record_inbound(
        MessageEvent(
            tentacle_id=channel.id,
            chat_type=address.chat_type,
            chat_id=address.chat_id,
            channel_thread_id=address.channel_thread_id,
            user_id=address.user_id,
            sender=UserProfile(channel_user_id=address.user_id, user_id=user.id),
            segments=[TextSegment(data={"text": "Earlier chat context"})],
        )
    )
    conversation = await app.conversations.ensure(thread.id, agent_tentacle_id=agent.id)
    await app.threads.record_handoff(
        thread, to_agent_tentacle_id=agent.id, to_model="test"
    )
    context = CommandContext(
        agent_id=agent.id,
        user_id=user.id,
        address=address,
        cwd=app.workspaces.open(thread.id, None).path,
        conversation=conversation,
        model="test",
        permission_mode=agent.default_permission_mode,
    )
    signal = CommandSignal(
        context,
        CommandInvocation(command_id="skill", arguments='  "raw argument"\n--flag=✓  '),
        "command-1",
    )
    return app, agent, channel, signal


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("gateway", [False, True])
async def test_direct_command_delivers_feedback_without_model_history_and_replays_once(
    scenario: tuple[Octomate, ExecutingAgent, FakeChannelTentacle, CommandSignal],
    stream: bool,
    gateway: bool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, agent, channel, signal = scenario
    channel.config.stream = ChannelStreamConfig(enabled=stream)
    agent.gateway = gateway
    capabilities = AsyncMock(return_value=[])
    monkeypatch.setattr(agent, "user_capabilities", capabilities)
    state = ReflexState()
    await reflex_graph.run(
        inputs=Awake(signal),
        state=state,
        deps=ReflexDeps(
            workspaces=app.workspaces,
            agents=app.agents,
            channels=app.channels,
            conversation_manager=app.conversations,
            thread_manager=app.threads,
            action_manager=app.deferred_actions,
            gateway=app.gateway,
        ),
    )
    assert state.decision is None
    await app.kick(signal)
    assert agent.invocations == [signal.invocation]
    assert not agent.turns
    assert not agent.streams
    assert not app.gateway.sessions
    assert any(
        message.get("text") == "Done"
        for _, _, messages, *_ in channel.sent
        for message in messages
    )
    conversation = signal.context.conversation
    assert conversation is not None
    stored = await app.conversations.get(conversation.id)
    assert len(await stored.runs) == 0
    receipt = await app.threads.find_message(
        conversation.thread_id, signal.delivery_id, "inbound"
    )
    assert isinstance(receipt, ThreadCommand)
    assert isinstance(receipt.outcome, CommandResult)
    capabilities.assert_awaited_once()
    assert isinstance(agent.suspender, ReflexSuspender)
    assert agent.suspender.decision is None
    assert (
        bool(
            [
                cap
                for cap in agent.capabilities or []
                if isinstance(cap, GatewayCapability)
            ]
        )
        is gateway
    )
    assert isinstance(app.workspaces, RecordingWorkspaceManager)
    assert app.workspaces.saved == [conversation.thread_id]


@pytest.mark.parametrize("stream", [False, True])
async def test_command_agent_stream_is_recorded_bound_and_delivered_without_another_run(
    scenario: tuple[Octomate, ExecutingAgent, FakeChannelTentacle, CommandSignal],
    stream: bool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, agent, channel, signal = scenario
    channel.config.stream = ChannelStreamConfig(enabled=stream)
    conversation = signal.context.conversation
    assert conversation is not None
    result = AgentRunResult[ChannelOutput]("Command reply")

    async def events() -> AsyncGenerator[ReactStreamEvent[ChannelOutput], None]:
        assert app.gateway.get(conversation.id) is not None
        assert isinstance(agent.suspender, ReflexSuspender)
        assert agent.suspender.run_name == "command"
        assert agent.suspender.decision is None
        await app.conversations.record_agent_run(
            conversation,
            result.run_id,
            [
                ModelRequest(
                    parts=[
                        UserPromptPart(content=f"/review {signal.invocation.arguments}")
                    ]
                ),
                ModelResponse(parts=[TextPart(content="Command reply")]),
            ],
        )
        yield AgentRunResultEvent(result)
        assert agent.suspender.decision is not None
        assert agent.suspender.decision.agent_id == agent.id
        assert agent.suspender.decision.model == signal.context.model

    agent.behavior = "stream_complete"
    monkeypatch.setattr(agent, "events", events)
    await app.kick(signal)
    await app.kick(signal)
    assert agent.invocations == [signal.invocation]
    assert not agent.turns
    assert not agent.streams
    assert not app.gateway.sessions
    thread = await app.threads.get(conversation.thread_id)
    assert thread is not None
    replies = [
        message for message in thread.messages if message.direction == "outbound"
    ]
    assert len(replies) == 1
    assert replies[0].message_text == "Command reply"
    bound = await app.threads.related_model_messages(replies[0].id)
    assert len(bound) == 1
    assert bound[0].run_id == result.run_id
    assert any(
        message.get("text") == "Command reply"
        for _, _, messages, *_ in channel.sent
        for message in messages
    )
    receipt = await app.threads.find_message(thread.id, signal.delivery_id, "inbound")
    assert isinstance(receipt, ThreadCommand)
    assert receipt.outcome == CommandResult()


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("defer_first", [False, True])
async def test_command_defers_through_reflex_then_resumes_as_an_ordinary_agent_run(
    scenario: tuple[Octomate, ExecutingAgent, FakeChannelTentacle, CommandSignal],
    stream: bool,
    defer_first: bool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, agent, channel, signal = scenario
    channel.config.stream = ChannelStreamConfig(enabled=stream)
    conversation = signal.context.conversation
    assert conversation is not None
    thread = await app.threads.get(conversation.thread_id, with_messages=False)
    assert thread is not None
    agent.models["opus"] = "selected-model"
    await app.threads.record_handoff(
        thread, to_agent_tentacle_id=agent.id, to_model="opus"
    )
    signal = replace(signal, context=replace(signal.context, model="opus"))
    requests = DeferredToolRequests(
        approvals=[
            ToolCallPart(
                tool_name="write_file", args={"path": "draft.txt"}, tool_call_id="write"
            )
        ]
    )

    async def events() -> AsyncGenerator[ReactStreamEvent[ChannelOutput], None]:
        assert isinstance(agent.suspender, ReflexSuspender)
        assert agent.suspender.decision is None
        if not defer_first:
            yield MessageSentEvent(segments=[TextSegment(data={"text": "Working"})])
        event = await agent.suspender.suspend(requests)
        if event is not None:
            yield event
        yield AgentRunResultEvent(AgentRunResult(requests))

    agent.behavior = "stream_complete"
    monkeypatch.setattr(agent, "events", events)
    await app.kick(signal)
    assert isinstance(agent.suspender, ReflexSuspender)
    batch_id = agent.suspender.suspended_batch_id
    assert batch_id is not None
    batch = await app.deferred_actions.get_batch(batch_id)
    assert batch.run_name == "command"
    assert batch.decision is not None
    assert batch.decision.agent_id == agent.id
    assert batch.decision.model == signal.context.model
    assert signal.context.conversation is not None
    assert batch.conversation_id == signal.context.conversation.id
    approval = next(iter(batch.approvals))
    agent.reception_output = "Approved"
    agent.allow_reception_run = True
    await app.kick(
        DeferredActionBatchResponse(
            batch_id=batch_id,
            responder_id=signal.context.address.user_id,
            approvals={approval.id: True},
        )
    )
    runs = [*agent.turns, *agent.streams]
    assert len(runs) == 1
    assert runs[0].run_name == "resume"
    assert runs[0].model == "selected-model"
    assert len(agent.invocations) == 1


@pytest.mark.parametrize("stream", [False, True])
async def test_command_teleport_before_first_event_continues_the_agent_run(
    scenario: tuple[Octomate, ExecutingAgent, FakeChannelTentacle, CommandSignal],
    stream: bool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, agent, channel, signal = scenario
    channel.config.stream = ChannelStreamConfig(enabled=stream)
    requests = DeferredToolRequests(
        calls=[ToolCallPart(tool_name="teleport", args={}, tool_call_id="move")],
        metadata={"move": {"kind": TELEPORT_DEFER_KIND, "here": True}},
    )

    async def events() -> AsyncGenerator[ReactStreamEvent[ChannelOutput], None]:
        assert isinstance(agent.suspender, ReflexSuspender)
        assert agent.suspender.decision is None
        await agent.suspender.suspend(requests)
        yield AgentRunResultEvent(AgentRunResult(requests))

    agent.behavior = "stream_complete"
    agent.reception_output = "Continued"
    agent.allow_reception_run = True
    monkeypatch.setattr(agent, "events", events)
    await app.kick(signal)
    runs = [*agent.turns, *agent.streams]
    assert len(runs) == 1
    assert runs[0].run_name == "teleport"
    assert signal.context.conversation is not None
    assert runs[0].thread_id == signal.context.conversation.thread_id
    assert runs[0].model == agent.models["test"]
    assert runs[0].deferred_results is not None
    assert "move" in runs[0].deferred_results.calls
    assert len(agent.invocations) == 1
    assert not app.gateway.sessions


@pytest.mark.parametrize("stream", [False, True])
async def test_command_gateway_handoff_continues_in_the_same_reflex_graph(
    scenario: tuple[Octomate, ExecutingAgent, FakeChannelTentacle, CommandSignal],
    stream: bool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, agent, channel, signal = scenario
    channel.config.stream = ChannelStreamConfig(enabled=stream)
    other = app.connect(
        FakeAgent(id="other", reception_output="Handed over", allow_reception_run=True)
    )
    channel.config.agents.append(other.id)
    conversation = signal.context.conversation
    assert conversation is not None

    async def events() -> AsyncGenerator[ReactStreamEvent[ChannelOutput], None]:
        session = app.gateway.get(conversation.id)
        assert session is not None
        session.decision = SummonDecision(
            agent_id=other.id,
            model="opus",
            destination=HereLanding(),
            reason="Continue with another agent",
            hint="Handing over",
            summon="Continue this review",
        )
        yield AgentRunResultEvent(AgentRunResult(""))

    agent.behavior = "stream_complete"
    monkeypatch.setattr(agent, "events", events)
    state = ReflexState()
    deps = ReflexDeps(
        workspaces=app.workspaces,
        agents=app.agents,
        channels=app.channels,
        conversation_manager=app.conversations,
        thread_manager=app.threads,
        action_manager=app.deferred_actions,
        gateway=app.gateway,
    )
    await reflex_graph.run(inputs=Awake(signal), state=state, deps=deps)
    assert len(agent.invocations) == 1
    assert not agent.turns
    assert not agent.streams
    runs = [*other.turns, *other.streams]
    assert len(runs) == 1
    assert runs[0].prompt == "Continue this review"
    command_gateway = next(
        cap for cap in agent.capabilities or [] if isinstance(cap, GatewayCapability)
    )
    handoff_gateway = next(
        cap for cap in runs[0].capabilities if isinstance(cap, GatewayCapability)
    )
    assert handoff_gateway.session is not command_gateway.session
    assert command_gateway.session.current_agent_id == agent.id
    assert command_gateway.session.conversation_id == conversation.id
    assert handoff_gateway.session.current_agent_id == other.id
    assert handoff_gateway.session.conversation_id != conversation.id
    assert state.selection == AgentRouteKey(other.id, "opus")
    assert state.conversation_id == handoff_gateway.session.conversation_id
    assert not app.gateway.sessions


@pytest.mark.parametrize("reason", ["busy", "unknown", "unavailable"])
async def test_refused_command_does_not_mount_user_tools_or_save_a_workspace(
    scenario: tuple[Octomate, ExecutingAgent, FakeChannelTentacle, CommandSignal],
    reason: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, agent, channel, signal = scenario
    conversation = signal.context.conversation
    assert conversation is not None
    if reason == "busy":
        app.gateway.sessions[conversation.id] = None
    elif reason == "unknown":
        signal = replace(signal, invocation=CommandInvocation(command_id="missing"))
    else:
        channel.config.agents = []
    capabilities = AsyncMock(return_value=[])
    monkeypatch.setattr(agent, "user_capabilities", capabilities)
    await app.kick(signal)
    assert not agent.invocations
    capabilities.assert_not_awaited()
    assert isinstance(app.workspaces, RecordingWorkspaceManager)
    assert not app.workspaces.saved
    if reason == "busy":
        assert conversation.id in app.gateway.sessions
        assert app.gateway.sessions[conversation.id] is None


@pytest.mark.parametrize("cancel", [False, True])
async def test_user_preparation_failure_releases_guard_without_accepting_command(
    scenario: tuple[Octomate, ExecutingAgent, FakeChannelTentacle, CommandSignal],
    cancel: bool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, agent, _channel, signal = scenario
    error = asyncio.CancelledError if cancel else RuntimeError
    with monkeypatch.context() as patch:
        patch.setattr(agent, "user_capabilities", AsyncMock(side_effect=error))
        with pytest.raises(error):
            await app.kick(signal)
    assert not agent.invocations
    assert not app.gateway.sessions
    async with async_session() as session:
        assert await session.count(ThreadCommand) == 0
    await app.kick(signal)
    assert agent.invocations == [signal.invocation]


@pytest.mark.parametrize("cancel", [False, True])
async def test_command_failure_closes_native_stream_before_releasing_guard(
    scenario: tuple[Octomate, ExecutingAgent, FakeChannelTentacle, CommandSignal],
    cancel: bool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, agent, _channel, signal = scenario
    conversation = signal.context.conversation
    assert conversation is not None
    closed = asyncio.Event()

    async def events() -> AsyncGenerator[ReactStreamEvent[ChannelOutput], None]:
        owner = asyncio.current_task()
        try:
            yield MessageSentEvent(segments=[TextSegment(data={"text": "Working"})])
            if cancel:
                raise asyncio.CancelledError
            raise RuntimeError("Native failure")
        finally:
            assert asyncio.current_task() is owner
            assert conversation.id in app.gateway.sessions
            closed.set()

    agent.behavior = "stream_complete"
    monkeypatch.setattr(agent, "events", events)
    with pytest.raises(asyncio.CancelledError if cancel else RuntimeError):
        await app.kick(signal)
    assert closed.is_set()
    assert not app.gateway.sessions
    receipt = await app.threads.find_message(
        conversation.thread_id, signal.delivery_id, "inbound"
    )
    assert isinstance(receipt, ThreadCommand)
    assert isinstance(receipt.outcome, CommandError)
    assert receipt.outcome.status == "failed"


@pytest.mark.parametrize("model_run", [False, True])
async def test_command_graph_can_deliver_to_trunklines_existing_request_sink(
    scenario: tuple[Octomate, ExecutingAgent, FakeChannelTentacle, CommandSignal],
    model_run: bool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, agent, _channel, signal = scenario
    channel = app.connect(
        TrunklineTentacle(
            "trunkline", app, config=TrunklineChannelConfig(agents=[agent.id])
        )
    )
    channel.self_profile = await channel.ink.inspect()
    address = replace(
        signal.context.address,
        channel_tentacle_id=channel.id,
        chat_id=str(signal.context.user_id),
        user_id=str(signal.context.user_id),
    )
    thread = await app.threads.ensure(address)
    await app.threads.record_inbound(
        MessageEvent(
            tentacle_id=channel.id,
            chat_type=address.chat_type,
            chat_id=address.chat_id,
            channel_thread_id=address.channel_thread_id,
            user_id=address.user_id,
            sender=UserProfile(
                channel_user_id=address.user_id, user_id=signal.context.user_id
            ),
        )
    )
    conversation = await app.conversations.ensure(thread.id, agent_tentacle_id=agent.id)
    await app.threads.record_handoff(
        thread, to_agent_tentacle_id=agent.id, to_model="test"
    )
    signal = replace(
        signal,
        context=replace(
            signal.context,
            address=address,
            conversation=conversation,
            cwd=app.workspaces.open(thread.id, None).path,
        ),
    )

    async def events() -> AsyncGenerator[ReactStreamEvent[ChannelOutput], None]:
        yield AgentRunResultEvent(AgentRunResult("Done"))

    if model_run:
        agent.behavior = "stream_complete"
        monkeypatch.setattr(agent, "events", events)
    send, receive = anyio.create_memory_object_stream[TrunklineStreamItem](10)
    async with send, receive:
        token = current_sink.set(send)
        try:
            await app.kick(signal)
        finally:
            current_sink.reset(token)
        await send.aclose()
        received = [event async for event in receive]
    assert len(received) == 1
    assert isinstance(
        received[0], AgentRunResultEvent if model_run else MessageSentEvent
    )
    assert len(agent.invocations) == 1
    assert not app.gateway.sessions
