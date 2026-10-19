"""Connection ownership and durable native identities, without synthetic history."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import AsyncMock

import acp
import pytest
from acp import schema
from anyio import Path as AsyncPath
from pydantic import JsonValue
from sqlalchemy.ext.asyncio import AsyncEngine
from uuid_utils.compat import uuid7

from octomate.database import async_session
from octomate.managers.conversation import ConversationManager
from octomate.managers.thread import ThreadManager
from octomate.managers.user import UserManager
from octomate.nerve import Nerve
from octomate.schemas.thread import Thread, ThreadMessage
from octomate.schemas.user import UserProfile
from octomate.tentacles.acp.broker import NativeAcpBroker
from octomate.tentacles.acp.client import AcpProvider, NativeAcpClient
from tests.agent.test_acp_client import Callbacks
from tests.agent.test_acp_client import adapter_environment as adapter_environment
from tests.support.managers import a_project, a_registry
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


async def test_provider_error_releases_prompt_admission(
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
    monkeypatch.setattr(
        owner.client.connection,
        "send_request",
        AsyncMock(side_effect=acp.RequestError.invalid_params()),
    )
    with pytest.raises(acp.RequestError, match="Invalid params"):
        await owner.prompt(request)
    assert not owner.sessions[response.session_id].prompting


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
