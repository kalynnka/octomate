"""Connection ownership, native identities and driven ACP history."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import AsyncMock

import acp
import pytest
from acp import schema
from acp.connection import StreamDirection, StreamEvent
from anyio import Path as AsyncPath
from pydantic import JsonValue
from pydantic_ai.messages import EnqueuedMessagesEvent, PartStartEvent, TextPart
from sqlalchemy.ext.asyncio import AsyncEngine
from uuid_utils.compat import uuid7

from octomate.capabilities.harness.events import (
    RunEndedEvent,
    RunErrorEvent,
    RunStartedEvent,
)
from octomate.database import async_session
from octomate.managers.conversation import ConversationManager
from octomate.managers.thread import ThreadManager
from octomate.managers.user import UserManager
from octomate.nerve import Nerve, ThreadTopic
from octomate.schemas.messages import ModelMessage, ModelRequest
from octomate.schemas.runs import AgentRun
from octomate.schemas.thread import MessageBinding, Thread, ThreadMessage
from octomate.schemas.user import UserProfile
from octomate.tentacles.acp.broker import NativeAcpBroker
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
                threads=ThreadManager(users=users, nerve=Nerve()),
                conversations=ConversationManager(),
                projects=projects,
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
    thread = await owner.threads.get(registered.native.key, with_messages=False)
    assert thread is not None
    assert thread.kind == "native_thread"
    project = owner.projects.get("project")
    assert project is not None
    assert thread.project_id == project.id
    assert registered.native.cwd == cwd
    assert response.field_meta == {"providerSession": True}
    conversation = await owner.conversations.get(
        registered.conversation.id, with_history=False
    )
    assert conversation.thread_id == thread.id
    assert conversation.agent_tentacle_id == owner.client.provider.native_id
    assert conversation.external_id == response.session_id
    async with async_session() as session:
        assert not await session.list(ThreadMessage)
    assert not await owner.threads.list_threads(user_id=owner.sender.user_id)
    assert not await AsyncPath(tmp_path / ".octomate").exists()


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
    sender = await owner.threads.users.native_profile(
        owner.client.provider.native_id, user.username
    )
    assert sender is not None
    other = NativeAcpBroker(
        owner.client, sender, owner.threads, owner.conversations, owner.projects
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
    await owner.threads.record_native_prompt(
        registered.native.key.address(owner.sender.channel_user_id),
        message_id=str(uuid7()),
        text="private history",
        sender=owner.sender,
        timestamp=datetime.now(UTC),
    )
    user = await a_user("bob")
    sender = await owner.threads.users.native_profile(
        owner.client.provider.native_id, user.username
    )
    assert sender is not None
    other = NativeAcpBroker(
        owner.client, sender, owner.threads, owner.conversations, owner.projects
    )
    monkeypatch.setattr(
        NativeAcpClient, "new_session", AsyncMock(return_value=registered.native)
    )
    with pytest.raises(ValueError, match="already exists"):
        await other.new_session(
            schema.NewSessionRequest(cwd="/project", mcp_servers=[])
        )
    assert not other.sessions
    assert await owner.threads.get(registered.native.key, user_id=user.id) is None
    original = await owner.threads.get(
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
    sender = await owner.threads.users.native_profile(
        owner.client.provider.native_id, user.username
    )
    assert sender is not None
    other = NativeAcpBroker(
        owner.client, sender, owner.threads, owner.conversations, owner.projects
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
) -> None:
    owner, callbacks = broker
    response = await owner.new_session(
        schema.NewSessionRequest(cwd="/project", mcp_servers=[])
    )
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
        with pytest.raises(acp.RequestError, match="Invalid request"):
            await owner.prompt(request)
        assert callbacks.updates
        await owner.cancel(schema.CancelNotification(session_id=response.session_id))
        assert (await prompt).stop_reason == "cancelled"
    assert not owner.sessions[response.session_id].prompting
    async with async_session() as session:
        [run] = await session.list(AgentRun)
        [message] = await session.list(ModelRequest)
        assert message.metadata is not None
        assert message.metadata["stop_reason"] == "cancelled"
        assert len(run.messages) == 2


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
        with pytest.raises(acp.RequestError, match="Invalid request"):
            await owner.prompt(request)
        settled.set()
        with pytest.raises(asyncio.CancelledError):
            await prompt
    assert not owner.sessions[response.session_id].prompting


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
    with owner.threads.nerve.subscribe(ThreadTopic(registered.native.key)) as live:
        with pytest.raises(acp.RequestError, match="Invalid params"):
            await owner.prompt(request)
        events = [impulse.event for impulse in held(live)]
    assert isinstance(events[0], RunStartedEvent)
    assert isinstance(events[1], EnqueuedMessagesEvent)
    assert isinstance(events[-2], RunErrorEvent)
    assert isinstance(events[-1], RunEndedEvent)
    assert len(events) == (6 if partial else 4)
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
    assert not owner.sessions[response.session_id].prompting


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
    with owner.threads.nerve.subscribe(ThreadTopic(registered.native.key)) as live:
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
    assert sum(isinstance(event, RunStartedEvent) for event in events) == 2
    assert sum(isinstance(event, RunEndedEvent) for event in events) == 2
    assert [
        event.part.content
        for event in events
        if isinstance(event, PartStartEvent) and isinstance(event.part, TextPart)
    ] == ["x" * 100_000] * 2
    visible = await owner.threads.list_threads(user_id=owner.sender.user_id)
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


async def test_history_failure_does_not_advertise_a_committed_end(
    broker: tuple[NativeAcpBroker, Callbacks], monkeypatch: pytest.MonkeyPatch
) -> None:
    owner, callbacks = broker
    response = await owner.new_session(
        schema.NewSessionRequest(cwd="/project", mcp_servers=[])
    )
    callbacks.answered.set()
    monkeypatch.setattr(
        owner.conversations,
        "record_agent_run",
        AsyncMock(side_effect=ValueError("storage refused")),
    )
    registered = owner.sessions[response.session_id]
    with owner.threads.nerve.subscribe(ThreadTopic(registered.native.key)) as live:
        with pytest.raises(ValueError, match="storage refused"):
            await owner.prompt(
                schema.PromptRequest(
                    session_id=response.session_id, prompt=[acp.text_block("hello")]
                )
            )
        assert not any(
            isinstance(impulse.event, RunEndedEvent) for impulse in held(live)
        )
    assert not registered.prompting


async def test_broker_requires_an_owned_matching_native_profile(
    broker: tuple[NativeAcpBroker, Callbacks],
) -> None:
    owner, _ = broker
    with pytest.raises(ValueError, match="registered session owner"):
        NativeAcpBroker(
            owner.client,
            UserProfile(),
            owner.threads,
            owner.conversations,
            owner.projects,
        )
    with pytest.raises(ValueError, match="another native runtime"):
        NativeAcpBroker(
            owner.client,
            UserProfile(user_id=owner.sender.user_id, channel_tentacle_id="different"),
            owner.threads,
            owner.conversations,
            owner.projects,
        )
