"""Connection ownership, native identities and driven ACP history."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import acp
import pytest
from acp import schema
from acp.connection import StreamDirection, StreamEvent
from anyio import Path as AsyncPath
from pydantic import JsonValue
from pydantic_ai.messages import EnqueuedMessagesEvent, PartStartEvent, TextPart
from sqlalchemy.ext.asyncio import AsyncEngine
from uuid_utils.compat import uuid7

from octomate import Octomate
from octomate.capabilities.harness.events import (
    ReflexAsleepEvent,
    ReflexAwakeEvent,
    RunEndedEvent,
    RunErrorEvent,
    RunStartedEvent,
)
from octomate.database import async_session
from octomate.managers.user import UserManager
from octomate.managers.workspaces import WorkspaceManager
from octomate.nerve import ThreadTopic
from octomate.schemas.awakes import NativeGatewaySignal, StillAwake, UserMessageSignal
from octomate.schemas.events import MessageEvent
from octomate.schemas.messages import ModelMessage, ModelRequest
from octomate.schemas.runs import AgentRun
from octomate.schemas.thread import MessageBinding, Thread, ThreadMessage
from octomate.schemas.triage import SchemeDecision
from octomate.schemas.user import UserProfile
from octomate.tentacles.acp.base import AcpTentacle
from octomate.tentacles.acp.broker import NativeAcpBroker, RegisteredAcpSession
from octomate.tentacles.acp.client import AcpProvider, NativeAcpClient
from tests.agent.test_acp_client import Callbacks
from tests.agent.test_acp_client import adapter_environment as adapter_environment
from tests.support.managers import a_project, a_registry
from tests.support.nerve import held
from tests.support.users import a_user


@pytest.fixture(params=list(AcpProvider))
async def broker(
    request: pytest.FixtureRequest,
    adapter_environment: dict[str, str],
    in_memory_engine: AsyncEngine,
    tmp_path: Path,
) -> AsyncGenerator[tuple[NativeAcpBroker, Callbacks]]:
    provider = AcpProvider(request.param)
    user = await a_user("alice")
    users = UserManager()
    sender = await users.native_profile(provider.native_id, user.username)
    assert sender is not None
    root = tmp_path / "project"
    await AsyncPath(root).mkdir()
    projects = await a_registry(a_project(root, name="project"))
    app = Octomate(users=users, workspaces=WorkspaceManager(projects=projects))
    tentacle = app.connect(AcpTentacle(id="acp", octomate=app))
    callbacks = Callbacks()
    async with NativeAcpClient.connect(
        provider,
        callbacks,
        schema.InitializeRequest(protocol_version=1),
        env=adapter_environment,
    ) as client:
        yield (
            NativeAcpBroker(
                client=client,
                sender=sender,
                tentacle=tentacle,
            ),
            callbacks,
        )


async def test_registers_native_identity_without_history_or_managed_workspace(
    broker: tuple[NativeAcpBroker, Callbacks],
    tmp_path: Path,
) -> None:
    owner, _ = broker
    cwd = str(tmp_path / "project")
    response = await owner.new_session(
        schema.NewSessionRequest(cwd=cwd, mcp_servers=[])
    )
    registered = owner.sessions[response.session_id]
    thread = await owner.app.threads.get(registered.native.key, with_messages=False)
    assert thread is not None
    assert thread.kind == "native_thread"
    project = owner.app.projects.get("project")
    assert project is not None
    assert thread.project_id == project.id
    assert registered.native.cwd == cwd
    assert response.field_meta == {"providerSession": True}
    conversation = await owner.app.conversations.get(
        registered.conversation.id, with_history=False
    )
    assert conversation.thread_id == thread.id
    assert conversation.agent_tentacle_id == owner.client.provider.native_id
    assert conversation.external_id == response.session_id
    async with async_session() as session:
        assert not await session.list(ThreadMessage)
    assert not await owner.app.threads.list_threads(user_id=owner.sender.user_id)
    assert not await AsyncPath(owner.app.workspaces.workspaces_dir).exists()
    assert not owner.app.awakes


async def test_unknown_session_never_reaches_provider(
    broker: tuple[NativeAcpBroker, Callbacks],
    adapter_environment: dict[str, str],
) -> None:
    owner, _ = broker
    with pytest.raises(acp.RequestError, match="Invalid params"):
        await owner.prompt(
            schema.PromptRequest(
                session_id="someone-else", prompt=[acp.text_block("hello")]
            )
        )
    await owner.cancel(schema.CancelNotification(session_id="someone-else"))
    log = await AsyncPath(adapter_environment["ACP_TEST_LOG"]).read_text()
    assert len(log.splitlines()) == 1
    async with async_session() as session:
        assert not await session.list(Thread)


async def test_another_broker_cannot_drive_an_owned_session(
    broker: tuple[NativeAcpBroker, Callbacks],
) -> None:
    owner, _ = broker
    response = await owner.new_session(
        schema.NewSessionRequest(cwd="/project", mcp_servers=[])
    )
    user = await a_user("bob")
    sender = await owner.app.threads.users.native_profile(
        owner.client.provider.native_id, user.username
    )
    assert sender is not None
    other = NativeAcpBroker(
        owner.client,
        sender,
        owner.tentacle,
    )
    with pytest.raises(acp.RequestError, match="Invalid params"):
        await other.prompt(
            schema.PromptRequest(
                session_id=response.session_id, prompt=[acp.text_block("hello")]
            )
        )
    with pytest.raises(acp.RequestError, match="Invalid params"):
        await other.set_mode(
            schema.SetSessionModeRequest(session_id=response.session_id, mode_id="plan")
        )
    with pytest.raises(acp.RequestError, match="Invalid params"):
        await other.set_config_option(
            schema.SetSessionConfigOptionSelectRequest(
                session_id=response.session_id, config_id="mode", value="plan"
            )
        )
    await other.cancel(schema.CancelNotification(session_id=response.session_id))
    assert not other.sessions


async def test_existing_native_history_cannot_be_claimed_as_a_new_session(
    broker: tuple[NativeAcpBroker, Callbacks],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    owner, _ = broker
    response = await owner.new_session(
        schema.NewSessionRequest(cwd="/project", mcp_servers=[])
    )
    registered = owner.sessions[response.session_id]
    await owner.app.threads.record_native_prompt(
        registered.native.key.address(owner.sender.channel_user_id),
        message_id=str(uuid7()),
        text="private history",
        sender=owner.sender,
        timestamp=datetime.now(UTC),
    )
    user = await a_user("bob")
    sender = await owner.app.threads.users.native_profile(
        owner.client.provider.native_id, user.username
    )
    assert sender is not None
    other = NativeAcpBroker(
        owner.client,
        sender,
        owner.tentacle,
    )
    monkeypatch.setattr(
        NativeAcpClient, "new_session", AsyncMock(return_value=registered.native)
    )
    with pytest.raises(ValueError, match="already exists"):
        await other.new_session(
            schema.NewSessionRequest(cwd="/project", mcp_servers=[])
        )
    assert not other.sessions
    assert await owner.app.threads.get(registered.native.key, user_id=user.id) is None
    original = await owner.app.threads.get(
        registered.native.key, user_id=owner.sender.user_id
    )
    assert original is not None
    assert [message.message_text for message in original.messages] == [
        "private history"
    ]


async def test_concurrent_registration_has_only_one_owner(
    broker: tuple[NativeAcpBroker, Callbacks],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    owner, _ = broker
    request = schema.NewSessionRequest(cwd="/project", mcp_servers=[])
    native = await owner.client.new_session(request)
    user = await a_user("bob")
    sender = await owner.app.threads.users.native_profile(
        owner.client.provider.native_id, user.username
    )
    assert sender is not None
    other = NativeAcpBroker(
        owner.client,
        sender,
        owner.tentacle,
    )
    monkeypatch.setattr(NativeAcpClient, "new_session", AsyncMock(return_value=native))
    results = await asyncio.gather(
        owner.new_session(request), other.new_session(request), return_exceptions=True
    )
    assert sum(isinstance(result, schema.NewSessionResponse) for result in results) == 1
    assert sum(isinstance(result, ValueError) for result in results) == 1
    assert len(owner.sessions) + len(other.sessions) == 1
    async with async_session() as session:
        assert len(await session.list(Thread)) == 1


async def test_busy_prompt_is_rejected_while_cancellation_and_updates_work(
    broker: tuple[NativeAcpBroker, Callbacks],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    owner, callbacks = broker
    response = await owner.new_session(
        schema.NewSessionRequest(cwd="/project", mcp_servers=[])
    )
    registered = owner.sessions[response.session_id]
    agent = owner.app.agents[owner.tentacle.id]
    assert agent is owner.tentacle
    assert list(owner.app.agents) == [owner.tentacle.id]
    assert agent.id not in owner.app.channels
    assert agent.models == {}
    kick = AsyncMock(wraps=owner.app.kick)
    stream = MagicMock(wraps=agent.run_stream_events)
    monkeypatch.setattr(owner.app, "kick", kick)
    monkeypatch.setattr(agent, "run_stream_events", stream)
    assert not owner.app.awakes
    request = schema.PromptRequest(
        session_id=response.session_id,
        prompt=[
            acp.text_block("Describe this"),
            acp.image_block("aW1hZ2U=", "image/png"),
        ],
        field_meta={"session_id": "extension, not the target"},
    )
    async with asyncio.timeout(10), asyncio.TaskGroup() as tasks:
        prompt = tasks.create_task(owner.prompt(request))
        await callbacks.asked.wait()
        kick.assert_awaited_once()
        signal = kick.call_args.args[0]
        assert isinstance(signal, UserMessageSignal)
        assert signal.agent_id == owner.tentacle.id
        assert len(signal.messages) == 1
        assert signal.messages[0].raw == request.model_dump_json(
            by_alias=True, exclude_none=True
        )
        stream.assert_called_once()
        assert stream.call_args.kwargs["conversation_id"] == registered.conversation.id
        assert agent.driven_sessions == {
            f"{owner.client.provider.native_id}/{response.session_id}": 1
        }
        assert registered.native.key in owner.app.awakes
        assert registered.conversation.id in owner.app.gateway.sessions
        address = registered.native.key.address(owner.sender.channel_user_id)
        with pytest.raises(StillAwake):
            await owner.app.kick(
                NativeGatewaySignal(
                    decision=SchemeDecision(
                        hint="handoff", brief="continue", destination=address
                    ),
                    agent_id=owner.client.provider.native_id,
                    user_profile=owner.sender,
                    source=address,
                )
            )
        with pytest.raises(acp.RequestError, match="Invalid request"):
            await owner.prompt(request)
        assert callbacks.updates
        await owner.cancel(schema.CancelNotification(session_id=response.session_id))
        assert (await prompt).stop_reason == "cancelled"
    assert not owner.app.awakes
    assert not owner.app.gateway.sessions
    assert not agent.driven_sessions
    assert registered.turn is None
    assert registered.native.key in owner.tentacle.attachments
    async with async_session() as session:
        [run] = await session.list(AgentRun)
        [message] = await session.list(ModelRequest)
        assert message.metadata is not None
        assert message.metadata["stop_reason"] == "cancelled"
        assert len(run.messages) == 2


async def test_one_tentacle_drives_both_providers_with_the_same_session_id(
    broker: tuple[NativeAcpBroker, Callbacks],
    adapter_environment: dict[str, str],
    tmp_path: Path,
) -> None:
    owner, callbacks = broker
    provider = (
        AcpProvider.CODEX
        if owner.client.provider is AcpProvider.CLAUDE
        else AcpProvider.CLAUDE
    )
    sender = await owner.app.users.native_profile(provider.native_id, "alice")
    assert sender is not None
    other_callbacks = Callbacks()
    async with NativeAcpClient.connect(
        provider,
        other_callbacks,
        schema.InitializeRequest(protocol_version=1),
        env={**adapter_environment, "ACP_TEST_LOG": str(tmp_path / "other.log")},
    ) as client:
        other = NativeAcpBroker(client, sender, owner.tentacle)
        first = await owner.new_session(
            schema.NewSessionRequest(cwd="/first", mcp_servers=[])
        )
        second = await other.new_session(
            schema.NewSessionRequest(cwd="/second", mcp_servers=[])
        )
        assert first.session_id == second.session_id
        async with asyncio.timeout(10), asyncio.TaskGroup() as tasks:
            prompts = [
                tasks.create_task(
                    current.prompt(
                        schema.PromptRequest(session_id=first.session_id, prompt=[])
                    )
                )
                for current in (owner, other)
            ]
            await callbacks.asked.wait()
            await other_callbacks.asked.wait()
            assert list(owner.app.agents) == [owner.tentacle.id]
            assert len(owner.app.awakes) == 2
            assert owner.tentacle.driven_sessions == {
                f"{item.native_id}/{first.session_id}": 1 for item in AcpProvider
            }
            callbacks.answered.set()
            other_callbacks.answered.set()
        assert [prompt.result().stop_reason for prompt in prompts] == ["end_turn"] * 2
        other.close()
    assert not owner.app.awakes
    assert not owner.tentacle.driven_sessions
    async with async_session() as session:
        runs = await session.list(AgentRun)
        assert len(runs) == 2
        assert {run.native_id for run in runs} == {
            item.native_id for item in AcpProvider
        }
        assert {run.cwd for run in runs} == {Path("/first"), Path("/second")}
        assert len({run.conversation_id for run in runs}) == 2


async def test_local_cancellation_keeps_admission_until_peer_settles(
    broker: tuple[NativeAcpBroker, Callbacks],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    owner, _ = broker
    response = await owner.new_session(
        schema.NewSessionRequest(cwd="/project", mcp_servers=[])
    )
    request = schema.PromptRequest(
        session_id=response.session_id, prompt=[acp.text_block("hello")]
    )
    started, cancelled, settled = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def peer_prompt(method: str, params: JsonValue | None = None) -> JsonValue:
        started.set()
        await settled.wait()
        return {"stopReason": "cancelled"}

    async def peer_cancel(method: str, params: JsonValue | None = None) -> None:
        assert method == "session/cancel"
        cancelled.set()

    monkeypatch.setattr(owner.client.connection, "send_request", peer_prompt)
    monkeypatch.setattr(owner.client.connection, "send_notification", peer_cancel)
    async with asyncio.timeout(10), asyncio.TaskGroup() as tasks:
        prompt = tasks.create_task(owner.prompt(request))
        await started.wait()
        prompt.cancel()
        await cancelled.wait()
        registered = owner.sessions[response.session_id]
        assert registered.native.key in owner.app.awakes
        assert registered.conversation.id in owner.app.gateway.sessions
        with pytest.raises(acp.RequestError, match="Invalid request"):
            await owner.prompt(request)
        settled.set()
        with pytest.raises(asyncio.CancelledError):
            await prompt
    assert not owner.app.awakes
    assert not owner.app.gateway.sessions


@pytest.mark.parametrize("partial", [False, True])
async def test_provider_error_releases_prompt_admission(
    broker: tuple[NativeAcpBroker, Callbacks],
    monkeypatch: pytest.MonkeyPatch,
    partial: bool,
) -> None:
    owner, _ = broker
    response = await owner.new_session(
        schema.NewSessionRequest(cwd="/project", mcp_servers=[])
    )
    request = schema.PromptRequest(
        session_id=response.session_id, prompt=[acp.text_block("hello")]
    )

    async def peer_error(method: str, params: JsonValue | None = None) -> JsonValue:
        if partial:
            owner.observe(
                StreamEvent(
                    direction=StreamDirection.INCOMING,
                    message={
                        "method": "session/update",
                        "params": {
                            "sessionId": response.session_id,
                            "update": {
                                "sessionUpdate": "agent_message_chunk",
                                "content": {"type": "text", "text": "partial"},
                            },
                        },
                    },
                )
            )
        raise acp.RequestError.invalid_params()

    monkeypatch.setattr(owner.client.connection, "send_request", peer_error)
    registered = owner.sessions[response.session_id]
    with owner.app.threads.nerve.subscribe(ThreadTopic(registered.native.key)) as live:
        with pytest.raises(acp.RequestError, match="Invalid params"):
            await owner.prompt(request)
        events = [impulse.event for impulse in held(live)]
    assert isinstance(events[0], ReflexAwakeEvent)
    assert isinstance(events[1], RunStartedEvent)
    assert isinstance(events[2], EnqueuedMessagesEvent)
    assert isinstance(events[-3], RunEndedEvent)
    assert isinstance(events[-2], RunErrorEvent)
    assert isinstance(events[-1], ReflexAsleepEvent)
    assert len(events) == (8 if partial else 6)
    async with async_session() as session:
        [run] = await session.list(AgentRun)
        [message] = await session.list(ModelRequest)
        bindings = await session.list(MessageBinding)
        binding = next(b for b in bindings if b.kind == "request_source")
        assert len(bindings) == (2 if partial else 1)
        assert binding.run_id == run.id
        assert binding.model_message_id == message.id
        assert message.metadata is not None
        assert "error" in message.metadata
    assert not owner.app.awakes
    assert not owner.app.gateway.sessions


async def test_prompt_history_hierarchy_and_nerve_before_delayed_callbacks(
    broker: tuple[NativeAcpBroker, Callbacks], monkeypatch: pytest.MonkeyPatch
) -> None:
    owner, callbacks = broker
    response = await owner.new_session(
        schema.NewSessionRequest(cwd="/project", mcp_servers=[])
    )
    registered = owner.sessions[response.session_id]
    callbacks.answered.set()
    release = asyncio.Event()
    original = Callbacks.__call__

    async def delayed(
        self: Callbacks, method: str, params: JsonValue | None, notification: bool
    ) -> JsonValue | None:
        if notification:
            await release.wait()
        return await original(self, method, params, notification)

    monkeypatch.setattr(Callbacks, "__call__", delayed)
    with owner.app.threads.nerve.subscribe(ThreadTopic(registered.native.key)) as live:
        for text in ("first", "second"):
            result = await owner.prompt(
                schema.PromptRequest(
                    session_id=response.session_id, prompt=[acp.text_block(text)]
                )
            )
            assert result.stop_reason == "end_turn"
        impulses = held(live)
    assert not callbacks.updates
    release.set()
    assert all(impulse.origin == registered.native.key for impulse in impulses)
    events = [impulse.event for impulse in impulses]
    assert sum(isinstance(event, ReflexAwakeEvent) for event in events) == 2
    assert sum(isinstance(event, ReflexAsleepEvent) for event in events) == 2
    assert isinstance(events[0], ReflexAwakeEvent)
    assert isinstance(events[-1], ReflexAsleepEvent)
    assert not owner.app.awakes
    assert registered.native.key in owner.tentacle.attachments
    assert not await AsyncPath(owner.app.workspaces.workspaces_dir).exists()
    assert sum(isinstance(event, RunStartedEvent) for event in events) == 2
    assert sum(isinstance(event, RunEndedEvent) for event in events) == 2
    assert [
        event.part.content
        for event in events
        if isinstance(event, PartStartEvent) and isinstance(event.part, TextPart)
    ] == ["x" * 100_000] * 2
    visible = await owner.app.threads.list_threads(user_id=owner.sender.user_id)
    assert len(visible) == 1
    async with async_session() as session:
        runs = await session.list(AgentRun, order_bys=[AgentRun["id"]])
        messages = await session.list(ModelMessage, order_bys=[ModelMessage["id"]])
        ledger = await session.list(ThreadMessage, order_bys=[ThreadMessage["id"]])
        bindings = await session.list(MessageBinding)
    assert len(runs) == 2
    assert len(messages) == len(ledger) == len(bindings) == 4
    assert [entry.message_text for entry in ledger] == [
        "first",
        "x" * 100_000,
        "second",
        "x" * 100_000,
    ]
    for index, run in enumerate(runs):
        assert run.kind == "octomate"
        assert run.conversation_id == registered.conversation.id
        assert run.native_session_id == response.session_id
        assert run.native_id == owner.client.provider.native_id
        assert run.native_turn_id is None
        assert run.cwd == Path("/project")
        assert [m.run_id for m in messages[index * 2 : index * 2 + 2]] == [run.id] * 2
    assert {b.model_message_id for b in bindings} == {m.id for m in messages}
    assert {b.thread_message_id for b in bindings} == {m.id for m in ledger}


async def test_binary_reply_binding_and_unowned_updates(
    broker: tuple[NativeAcpBroker, Callbacks], monkeypatch: pytest.MonkeyPatch
) -> None:
    owner, _ = broker
    response = await owner.new_session(
        schema.NewSessionRequest(cwd="/project", mcp_servers=[])
    )

    async def peer_prompt(method: str, params: JsonValue | None = None) -> JsonValue:
        for session_id in ("another-session", response.session_id):
            owner.observe(
                StreamEvent(
                    direction=StreamDirection.INCOMING,
                    message={
                        "method": "session/update",
                        "params": {
                            "sessionId": session_id,
                            "update": {
                                "sessionUpdate": "agent_message_chunk",
                                "content": {
                                    "type": "image",
                                    "data": "aW1hZ2U=",
                                    "mimeType": "image/png",
                                },
                            },
                        },
                    },
                )
            )
        return {"stopReason": "end_turn"}

    monkeypatch.setattr(owner.client.connection, "send_request", peer_prompt)
    await owner.prompt(schema.PromptRequest(session_id=response.session_id, prompt=[]))
    async with async_session() as session:
        assert len(await session.list(AgentRun)) == 1
        assert len(await session.list(ModelMessage)) == 2
        assert len(await session.list(MessageBinding)) == 2
        ledger = await session.list(ThreadMessage, order_bys=[ThreadMessage["id"]])
    assert [entry.message_text for entry in ledger] == [None, "[image/png]"]


async def test_history_failure_ends_the_run_without_a_committed_reply(
    broker: tuple[NativeAcpBroker, Callbacks], monkeypatch: pytest.MonkeyPatch
) -> None:
    owner, callbacks = broker
    response = await owner.new_session(
        schema.NewSessionRequest(cwd="/project", mcp_servers=[])
    )
    callbacks.answered.set()
    monkeypatch.setattr(
        owner.app.conversations,
        "record_agent_run",
        AsyncMock(side_effect=ValueError("storage refused")),
    )
    registered = owner.sessions[response.session_id]
    with owner.app.threads.nerve.subscribe(ThreadTopic(registered.native.key)) as live:
        with pytest.raises(ValueError, match="storage refused"):
            await owner.prompt(
                schema.PromptRequest(
                    session_id=response.session_id, prompt=[acp.text_block("hello")]
                )
            )
        events = [impulse.event for impulse in held(live)]
        ended = [event for event in events if isinstance(event, RunEndedEvent)]
        assert len(ended) == 1
        assert ended[0].thread_message_id is None
        assert isinstance(events[-2], RunErrorEvent)
        assert isinstance(events[-1], ReflexAsleepEvent)
    assert not owner.app.awakes
    assert not owner.app.gateway.sessions


async def test_broker_requires_an_owned_matching_native_profile(
    broker: tuple[NativeAcpBroker, Callbacks],
) -> None:
    owner, _ = broker
    with pytest.raises(ValueError, match="registered session owner"):
        NativeAcpBroker(
            owner.client,
            UserProfile(),
            owner.tentacle,
        )

    with pytest.raises(ValueError, match="another native runtime"):
        NativeAcpBroker(
            owner.client,
            UserProfile(user_id=owner.sender.user_id, channel_tentacle_id="different"),
            owner.tentacle,
        )


@pytest.fixture
async def recorded_session(
    broker: tuple[NativeAcpBroker, Callbacks],
) -> RegisteredAcpSession:
    owner, _ = broker
    response = await owner.new_session(
        schema.NewSessionRequest(cwd="/project", mcp_servers=[])
    )
    registered = owner.sessions[response.session_id]
    await owner.app.threads.record_native_prompt(
        registered.native.key.address(owner.sender.channel_user_id),
        message_id=str(uuid7()),
        text="owned history",
        sender=owner.sender,
        timestamp=datetime.now(UTC),
    )
    owner.close()
    return registered


@pytest.mark.parametrize("method", ["load", "resume"])
async def test_reopen_requires_access_before_contacting_provider(
    broker: tuple[NativeAcpBroker, Callbacks],
    recorded_session: RegisteredAcpSession,
    monkeypatch: pytest.MonkeyPatch,
    method: str,
) -> None:
    owner, _ = broker
    user = await a_user("bob")
    sender = await owner.app.threads.users.native_profile(
        owner.client.provider.native_id, user.username
    )
    assert sender is not None
    other = NativeAcpBroker(
        owner.client,
        sender,
        owner.tentacle,
    )
    peer = AsyncMock()
    monkeypatch.setattr(owner.client.connection, "send_request", peer)
    request = (
        schema.LoadSessionRequest if method == "load" else schema.ResumeSessionRequest
    )
    for session_id in (recorded_session.native.session_id, "unknown"):
        with pytest.raises(acp.RequestError, match="Invalid params"):
            await other.reopen_session(
                request(session_id=session_id, cwd="/project", mcp_servers=[])
            )
    assert not other.sessions
    assert not owner.tentacle.attachments
    peer.assert_not_called()


async def test_listing_filters_pages_without_granting_authority(
    broker: tuple[NativeAcpBroker, Callbacks],
    recorded_session: RegisteredAcpSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    owner, _ = broker
    peer = AsyncMock(
        side_effect=[
            {
                "sessions": [
                    {"sessionId": "private", "cwd": "/private", "title": "secret"}
                ],
                "nextCursor": "next",
            },
            {
                "sessions": [
                    {
                        "sessionId": recorded_session.native.session_id,
                        "cwd": "/project",
                        "title": "mine",
                        "_meta": {"retained": True},
                    }
                ]
            },
        ]
    )
    monkeypatch.setattr(owner.client.connection, "send_request", peer)
    page = await owner.list_sessions(schema.ListSessionsRequest(cwd="/project"))
    assert page.sessions == []
    assert page.next_cursor == "next"
    page = await owner.list_sessions(
        schema.ListSessionsRequest(cwd="/project", cursor=page.next_cursor)
    )
    assert [item.session_id for item in page.sessions] == [
        recorded_session.native.session_id
    ]
    assert page.sessions[0].field_meta == {"retained": True}
    assert page.next_cursor is None
    assert peer.call_args.args == (
        "session/list",
        {"cwd": "/project", "cursor": "next"},
    )
    assert not owner.sessions
    with pytest.raises(acp.RequestError, match="Invalid params"):
        await owner.prompt(
            schema.PromptRequest(
                session_id=recorded_session.native.session_id, prompt=[]
            )
        )


@pytest.mark.parametrize("method", ["load", "resume"])
async def test_reopen_reservation_excludes_competing_connections_and_releases_on_error(
    broker: tuple[NativeAcpBroker, Callbacks],
    recorded_session: RegisteredAcpSession,
    monkeypatch: pytest.MonkeyPatch,
    method: str,
) -> None:
    owner, _ = broker
    other = NativeAcpBroker(
        owner.client,
        owner.sender,
        owner.tentacle,
    )
    started, release = asyncio.Event(), asyncio.Event()

    async def peer_open(method: str, params: JsonValue | None = None) -> JsonValue:
        started.set()
        await release.wait()
        raise acp.RequestError.invalid_params()

    monkeypatch.setattr(owner.client.connection, "send_request", peer_open)
    request_type = (
        schema.LoadSessionRequest if method == "load" else schema.ResumeSessionRequest
    )
    request = request_type(
        session_id=recorded_session.native.session_id, cwd="/project", mcp_servers=[]
    )
    opening = asyncio.create_task(owner.reopen_session(request))
    await started.wait()
    with pytest.raises(acp.RequestError, match="Invalid request"):
        await other.reopen_session(request)
    release.set()
    with pytest.raises(acp.RequestError, match="Invalid params"):
        await opening
    assert not owner.sessions
    assert not owner.tentacle.attachments
    monkeypatch.setattr(
        owner.client.connection, "send_request", AsyncMock(return_value={})
    )
    await other.reopen_session(request)
    assert (
        other.sessions[request.session_id].conversation.id
        == recorded_session.conversation.id
    )
    other.close()
    assert not owner.tentacle.attachments


async def test_cancelled_reopen_releases_reservation(
    broker: tuple[NativeAcpBroker, Callbacks],
    recorded_session: RegisteredAcpSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    owner, _ = broker
    started = asyncio.Event()

    async def peer_open(method: str, params: JsonValue | None = None) -> JsonValue:
        started.set()
        await asyncio.Event().wait()
        raise AssertionError("cancelled open continued")

    monkeypatch.setattr(owner.client.connection, "send_request", peer_open)
    opening = asyncio.create_task(
        owner.reopen_session(
            schema.LoadSessionRequest(
                session_id=recorded_session.native.session_id,
                cwd="/project",
                mcp_servers=[],
            )
        )
    )
    await started.wait()
    opening.cancel()
    with pytest.raises(asyncio.CancelledError):
        await opening
    assert not owner.tentacle.attachments
    assert not owner.sessions


async def test_reopen_rejects_unrecorded_and_mismatched_identities(
    broker: tuple[NativeAcpBroker, Callbacks],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    owner, _ = broker
    response = await owner.new_session(
        schema.NewSessionRequest(cwd="/project", mcp_servers=[])
    )
    registered = owner.sessions[response.session_id]
    owner.close()
    peer = AsyncMock()
    monkeypatch.setattr(owner.client.connection, "send_request", peer)
    request = schema.LoadSessionRequest(
        session_id=response.session_id, cwd="/project", mcp_servers=[]
    )
    with pytest.raises(acp.RequestError, match="Invalid params"):
        await owner.reopen_session(request)
    await owner.app.threads.record_native_prompt(
        registered.native.key.address(owner.sender.channel_user_id),
        message_id=str(uuid7()),
        text="owned",
        sender=owner.sender,
        timestamp=datetime.now(UTC),
    )
    await owner.app.conversations.set_external_id(
        registered.conversation, "different-native-session"
    )
    with pytest.raises(acp.RequestError, match="Invalid params"):
        await owner.reopen_session(request)
    peer.assert_not_called()


@pytest.mark.parametrize("method", ["load", "resume"])
async def test_reopen_does_not_duplicate_history_or_publish_replay(
    broker: tuple[NativeAcpBroker, Callbacks],
    recorded_session: RegisteredAcpSession,
    method: str,
) -> None:
    owner, _ = broker
    request_type = (
        schema.LoadSessionRequest if method == "load" else schema.ResumeSessionRequest
    )
    with owner.app.threads.nerve.subscribe(
        ThreadTopic(recorded_session.native.key)
    ) as live:
        await owner.reopen_session(
            request_type(
                session_id=recorded_session.native.session_id,
                cwd="/project",
                mcp_servers=[],
            )
        )
        assert held(live) == []
    async with async_session() as session:
        assert len(await session.list(ThreadMessage)) == 1
        assert await session.list(AgentRun) == []
    assert (
        owner.sessions[recorded_session.native.session_id].conversation.id
        == recorded_session.conversation.id
    )


@pytest.mark.parametrize("method", ["load", "resume"])
async def test_relative_reopen_workspace_never_reaches_provider(
    broker: tuple[NativeAcpBroker, Callbacks],
    recorded_session: RegisteredAcpSession,
    monkeypatch: pytest.MonkeyPatch,
    method: str,
) -> None:
    owner, _ = broker
    peer = AsyncMock()
    monkeypatch.setattr(owner.client.connection, "send_request", peer)
    request_type = (
        schema.LoadSessionRequest if method == "load" else schema.ResumeSessionRequest
    )
    with pytest.raises(ValueError, match="absolute"):
        await owner.reopen_session(
            request_type(
                session_id=recorded_session.native.session_id,
                cwd="/project",
                mcp_servers=[],
                additional_directories=["relative"],
            )
        )
    with pytest.raises(ValueError, match="absolute"):
        await owner.list_sessions(schema.ListSessionsRequest(cwd="relative"))
    peer.assert_not_called()


async def test_conversation_guard_refuses_acp_before_recording_or_execution(
    broker: tuple[NativeAcpBroker, Callbacks],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    owner, _ = broker
    response = await owner.new_session(
        schema.NewSessionRequest(cwd="/project", mcp_servers=[])
    )
    registered = owner.sessions[response.session_id]
    peer = AsyncMock()
    monkeypatch.setattr(owner.client.connection, "send_request", peer)
    with owner.app.nerve.subscribe(ThreadTopic(registered.native.key)) as live:
        async with owner.app.gateway.driving(
            None, conversation_id=registered.conversation.id
        ):
            with pytest.raises(RuntimeError, match="already has a turn"):
                await owner.prompt(
                    schema.PromptRequest(session_id=response.session_id, prompt=[])
                )
            assert registered.conversation.id in owner.app.gateway.sessions
        events = [impulse.event for impulse in held(live)]
    assert [type(event) for event in events] == [
        ReflexAwakeEvent,
        RunErrorEvent,
        ReflexAsleepEvent,
    ]
    assert not owner.app.awakes
    assert not owner.app.gateway.sessions
    peer.assert_not_called()
    async with async_session() as session:
        assert await session.list(ThreadMessage) == []
        assert await session.list(AgentRun) == []


async def test_attached_conversation_cannot_cross_threads(
    broker: tuple[NativeAcpBroker, Callbacks], monkeypatch: pytest.MonkeyPatch
) -> None:
    owner, _ = broker
    first = await owner.new_session(
        schema.NewSessionRequest(cwd="/first", mcp_servers=[])
    )
    second = await owner.new_session(
        schema.NewSessionRequest(cwd="/second", mcp_servers=[])
    )
    peer = AsyncMock()
    monkeypatch.setattr(owner.client.connection, "send_request", peer)
    owner.sessions[first.session_id].conversation = owner.sessions[
        second.session_id
    ].conversation
    with pytest.raises(ValueError, match="belongs to another thread"):
        await owner.prompt(
            schema.PromptRequest(
                session_id=first.session_id, prompt=[acp.text_block("hello")]
            )
        )
    peer.assert_not_called()
    assert not owner.app.awakes
    async with async_session() as session:
        assert await session.list(ThreadMessage) == []
        assert await session.list(AgentRun) == []


async def test_cancelling_before_the_graph_starts_releases_the_thread(
    broker: tuple[NativeAcpBroker, Callbacks],
) -> None:
    owner, callbacks = broker
    response = await owner.new_session(
        schema.NewSessionRequest(cwd="/project", mcp_servers=[])
    )
    registered = owner.sessions[response.session_id]
    request = schema.PromptRequest(session_id=response.session_id, prompt=[])
    with owner.app.nerve.subscribe(ThreadTopic(registered.native.key)) as live:
        task = await owner.app.kick(
            UserMessageSignal(
                [
                    MessageEvent(
                        tentacle_id=owner.client.provider.native_id,
                        chat_type="thread",
                        chat_id=response.session_id,
                        user_id=owner.sender.channel_user_id,
                        sender=owner.sender,
                    )
                ]
            )
        )
        assert task is not None
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert not owner.app.awakes
        assert held(live) == []
    callbacks.answered.set()
    assert (await owner.prompt(request)).stop_reason == "end_turn"
