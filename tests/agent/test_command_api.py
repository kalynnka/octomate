"""Command inspection and explicit execution through the authenticated HTTP API."""

import asyncio
import json
import uuid
from collections.abc import AsyncGenerator
from dataclasses import asdict, replace
from pathlib import Path
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import HTTPException
from jsonschema import Draft202012Validator, ValidationError
from pydantic import JsonValue, SecretStr, TypeAdapter
from pydantic_ai import AgentRunResult, AgentRunResultEvent
from pydantic_ai.messages import (
    ModelRequest,
    ModelResponse,
    PartDeltaEvent,
    PartStartEvent,
    TextPart,
    TextPartDelta,
    ToolCallPart,
    UnknownCustomEvent,
    UserPromptPart,
)
from pydantic_ai.result import FinalResult
from pydantic_ai.tools import DeferredToolRequests
from sqlalchemy.ext.asyncio import AsyncEngine
from starlette.types import Message, Scope
from uuid_utils.compat import uuid7

from octomate import Octomate
from octomate.auth import current_user
from octomate.capabilities.gateway import GatewayCapability
from octomate.capabilities.harness.events import (
    ActionBatchEvent,
    MessageSentEvent,
    RunResultEvent,
    RunStartedEvent,
)
from octomate.capabilities.harness.react import ReactStreamEvent
from octomate.commands import CommandStreamEvent, command_context, discover_commands
from octomate.config.auth import AuthConfig
from octomate.config.channels import TrunklineChannelConfig
from octomate.database import async_session
from octomate.dependencies import workspace_manager
from octomate.managers.auth import AuthManager
from octomate.managers.workspaces import WorkspaceManager
from octomate.reflex.suspender import ReflexSuspender
from octomate.schemas.commands import (
    CommandCatalog,
    CommandDescriptor,
    CommandError,
    CommandInvocation,
    CommandOutcomeEvent,
    CommandResult,
)
from octomate.schemas.conversation import ChannelAddress, Conversation
from octomate.schemas.events import MessageEvent
from octomate.schemas.segments import MessageSegment, TextSegment
from octomate.schemas.thread import Thread, ThreadCommand
from octomate.schemas.user import User, UserProfile
from octomate.tentacles.channel import ChannelOutput
from octomate.tentacles.trunkline import TrunklineTentacle
from octomate.types.permissions import PermissionMode
from tests.agent.test_command_execution import ExecutingAgent
from tests.agent.test_command_manager import DiscoveringAgent
from tests.support.channels import FakeChannelTentacle
from tests.support.managers import a_project, a_registry

command_event_adapter: TypeAdapter[CommandStreamEvent] = TypeAdapter(CommandStreamEvent)


def command_events(response: httpx.Response) -> list[CommandStreamEvent]:
    """Decode the endpoint's SSE data through the public event contract."""
    assert response.status_code == 200
    assert response.headers["content-type"] == "text/event-stream; charset=utf-8"
    return [
        command_event_adapter.validate_json(frame.removeprefix("data: "))
        for frame in response.text.strip().split("\n\n")
    ]


async def discover(
    *,
    app: Octomate,
    user: User,
    agent_id: str,
    address: ChannelAddress,
    conversation_id: uuid.UUID | None = None,
    model: str | None = None,
    permission_mode: str | None = None,
    refresh: bool = False,
    prefix: str = "",
) -> CommandCatalog:
    """Resolve the endpoint's dependency before directly exercising its async body."""
    context = await command_context(
        app=app,
        user=user,
        agent_id=agent_id,
        address=address,
        conversations=app.conversations,
        threads=app.threads,
        workspaces=app.workspaces,
        conversation_id=conversation_id,
        model=model,
        permission_mode=permission_mode,
    )
    return await discover_commands(
        context=context,
        app=app,
        refresh=refresh,
        prefix=prefix,
    )


@pytest.fixture
async def user(in_memory_engine: AsyncEngine) -> User:
    user = User(username="alice")
    async with async_session() as session:
        session.add(user)
        await session.commit()
    return user


@pytest.fixture
def address(request: pytest.FixtureRequest, user: User) -> ChannelAddress:
    if getattr(request, "param", "im") == "trunkline":
        return ChannelAddress(
            "trunkline", "thread", str(user.id), str(user.id), "topic"
        )
    return ChannelAddress("im", "thread", "group", "alice", "topic", shared=True)


@pytest.fixture
def agent() -> ExecutingAgent:
    return ExecutingAgent()


@pytest.fixture
async def app(user: User, agent: DiscoveringAgent, address: ChannelAddress) -> Octomate:
    app = Octomate()
    agent.commands = app.commands
    app.connect(agent)
    app.connect(FakeChannelTentacle(octomate=app))
    if address.channel_tentacle_id == "trunkline":
        trunkline = app.connect(
            TrunklineTentacle(
                "trunkline", app, config=TrunklineChannelConfig(agents=[agent.id])
            )
        )
        trunkline.self_profile = await trunkline.ink.inspect()
    app.dependency_overrides[current_user] = lambda: user
    return app


@pytest.fixture
async def conversation(
    app: Octomate, user: User, agent: DiscoveringAgent, address: ChannelAddress
) -> Conversation:
    thread = await app.threads.ensure(address)
    await app.threads.record_inbound(
        MessageEvent(
            tentacle_id=address.channel_tentacle_id,
            chat_id=address.chat_id,
            chat_type=address.chat_type,
            channel_thread_id=address.channel_thread_id,
            user_id=address.user_id,
            sender=UserProfile(channel_user_id=address.user_id, user_id=user.id),
        )
    )
    conversation = await app.conversations.ensure(thread.id, agent_tentacle_id=agent.id)
    await app.threads.record_handoff(
        thread, to_agent_tentacle_id=agent.id, to_model="test"
    )
    return conversation


async def test_composer_discovery_and_completion_create_no_rows_or_workspace(
    app: Octomate, user: User, agent: DiscoveringAgent, address: ChannelAddress
) -> None:
    agent.descriptors.add(CommandDescriptor(id="help", name="help", description="Help"))
    catalog = await discover(
        app=app,
        user=user,
        agent_id=agent.id,
        address=address,
        prefix="REV",
    )
    assert {item.name for item in catalog.descriptors} == {"review"}
    assert catalog.context.cwd is None
    assert catalog.context.conversation is None
    full = await discover(
        app=app,
        user=user,
        agent_id=agent.id,
        address=address,
    )
    assert len(full.descriptors) == 2
    assert len(agent.calls) == 1
    assert not agent.turns
    assert not agent.streams
    assert not app.workspaces.workspaces_dir.exists()
    async with async_session() as session:
        assert await session.list(Thread, limit=None) == []
        assert await session.list(Conversation, limit=None) == []


async def test_http_catalog_preserves_extensions_and_omits_runtime_history(
    app: Octomate,
    agent: DiscoveringAgent,
    address: ChannelAddress,
    conversation: Conversation,
) -> None:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post(
            "/api/commands/catalog",
            headers={"X-Octomate-Request": "1"},
            json={
                "agent_id": agent.id,
                "address": asdict(address),
                "conversation_id": str(conversation.id),
            },
        )
        assert response.status_code == 200, response.text
        data = response.json()
        assert data["descriptors"][0]["path"] == "skill"
        assert data["context"]["conversation"]["id"] == str(conversation.id)
        assert (
            not {"runs", "messages", "external_id", "allowed_tools"}
            & data["context"]["conversation"].keys()
        )
        schema = (await client.get("/openapi.json")).json()
        operation = schema["paths"]["/api/commands/catalog"]["post"]
        body_schema = operation["requestBody"]["content"]["application/json"]["schema"]
        fields = schema["components"]["schemas"][body_schema["$ref"].rsplit("/", 1)[-1]]
        assert set(fields["required"]) == {"agent_id", "address"}
        assert set(fields["properties"]) == {
            "agent_id",
            "address",
            "conversation_id",
            "model",
            "permission_mode",
            "refresh",
            "prefix",
        }
        assert response.headers["cache-control"] == "no-store"


async def test_http_uses_injected_managers(
    app: Octomate,
    address: ChannelAddress,
    conversation: Conversation,
    tmp_path: Path,
) -> None:
    workspaces = WorkspaceManager(workspaces_dir=tmp_path / "injected")
    app.dependency_overrides[workspace_manager] = lambda: workspaces
    body = {
        "agent_id": "inkling",
        "address": asdict(address),
        "conversation_id": str(conversation.id),
    }
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post(
            "/api/commands/catalog", headers={"X-Octomate-Request": "1"}, json=body
        )
        assert response.status_code == 200
        assert response.json()["context"]["cwd"] == str(
            workspaces.open(conversation.thread_id, None).path
        )


@pytest.mark.parametrize(
    ("authenticated", "header", "status"), [(False, True, 401), (True, False, 403)]
)
@pytest.mark.parametrize("endpoint", ["catalog", "execute"])
async def test_http_requires_login_and_browser_header(
    app: Octomate,
    address: ChannelAddress,
    authenticated: bool,
    header: bool,
    status: int,
    endpoint: str,
) -> None:
    if not authenticated:
        app.dependency_overrides.clear()
        app.auth = AuthManager(
            AuthConfig(
                access_token_salt=SecretStr("test-access-salt-only"),
                refresh_token_salt=SecretStr("test-refresh-salt-only"),
                api_key_salt=SecretStr("test-api-key-salt-only"),
            )
        )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post(
            f"/api/commands/{endpoint}",
            headers={"X-Octomate-Request": "1"} if header else {},
            json={
                "agent_id": "inkling",
                "address": asdict(address),
                "command_id": "skill",
                "delivery_id": "command-1",
            },
        )
    assert response.status_code == status
    assert response.headers["content-type"] == "application/json"


@pytest.mark.parametrize("field", ["cwd", "external_id", "user_id"])
async def test_http_ignores_runtime_context_overrides(
    app: Octomate,
    user: User,
    agent: DiscoveringAgent,
    address: ChannelAddress,
    field: str,
) -> None:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post(
            "/api/commands/catalog",
            headers={"X-Octomate-Request": "1"},
            json={"agent_id": "inkling", "address": asdict(address), field: "injected"},
        )
    assert response.status_code == 200
    assert agent.calls[0].user_id == user.id
    assert agent.calls[0].cwd is None
    assert agent.calls[0].conversation is None


async def test_cache_hit_rechecks_channel_agent_configuration(
    app: Octomate, user: User, agent: DiscoveringAgent, address: ChannelAddress
) -> None:
    result = await discover(
        app=app,
        user=user,
        agent_id=agent.id,
        address=address,
    )
    assert result.context.user_id == user.id
    assert await app.users.profile("im", address.user_id) is None
    app.channels["im"].config.agents = ["other"]
    with pytest.raises(HTTPException, match="agent"):
        await discover(
            app=app,
            user=user,
            agent_id=agent.id,
            address=address,
        )
    assert len(agent.calls) == 1


async def test_inspection_needs_no_profile_or_conversation_membership(
    app: Octomate,
    agent: DiscoveringAgent,
    address: ChannelAddress,
    conversation: Conversation,
) -> None:
    other = User(username="bob")
    async with async_session() as session:
        session.add(other)
        await session.commit()
    result = await discover(
        app=app,
        user=other,
        agent_id=agent.id,
        address=replace(address, user_id="bob"),
        conversation_id=conversation.id,
    )
    assert result.status == "ready"
    assert result.context.user_id == other.id
    assert result.context.conversation is not None
    assert result.context.conversation.id == conversation.id
    assert await app.users.profile("im", "bob") is None


@pytest.mark.parametrize(
    "change", ["route", "address", "missing", "subagent", "native", "unbound", "agent"]
)
async def test_inspection_only_requires_available_channel_agent_and_context(
    app: Octomate,
    user: User,
    agent: DiscoveringAgent,
    address: ChannelAddress,
    conversation: Conversation,
    change: str,
) -> None:
    conversation_id = conversation.id
    agent_id = agent.id
    if change == "route":
        await app.threads.record_handoff(address, to_agent_tentacle_id="other")
    elif change == "address":
        address = replace(address, channel_thread_id="elsewhere")
    elif change == "missing":
        conversation_id = uuid7()
    elif change == "subagent":
        child = await app.conversations.ensure(
            conversation.thread_id,
            agent_tentacle_id=agent.id,
            subagent_id="child",
            parent_conversation_id=conversation.id,
        )
        conversation_id = child.id
    elif change == "native":
        address = replace(address, channel_tentacle_id="codex-native")
    elif change == "unbound":
        app.channels["im"].config.agents = ["other"]
    elif change == "agent":
        agent_id = "missing"
    if change in {"route", "address", "subagent"}:
        result = await discover(
            app=app,
            user=user,
            agent_id=agent_id,
            address=address,
            conversation_id=conversation_id,
        )
        assert result.status == "ready"
        return
    with pytest.raises(HTTPException):
        await discover(
            app=app,
            user=user,
            agent_id=agent_id,
            address=address,
            conversation_id=conversation_id,
        )
    assert not agent.calls


async def test_workspace_and_session_are_resolved_again_before_cache_lookup(
    app: Octomate,
    user: User,
    agent: DiscoveringAgent,
    address: ChannelAddress,
    conversation: Conversation,
    tmp_path: Path,
) -> None:
    first = await discover(
        app=app,
        user=user,
        agent_id=agent.id,
        address=address,
        conversation_id=conversation.id,
    )
    assert first.context.cwd == app.workspaces.open(conversation.thread_id, None).path
    project = a_project(tmp_path / "project")
    app.workspaces.projects = await a_registry(project)
    await app.threads.bind(conversation.thread_id, project)
    async with async_session() as session:
        loaded = await session.get(Conversation, conversation.id)
        assert loaded is not None
        loaded.external_id = "new-runtime-session"
        await session.commit()
    second = await discover(
        app=app,
        user=user,
        agent_id=agent.id,
        address=address,
        conversation_id=conversation.id,
    )
    assert second.context.cwd == app.workspaces.path(conversation.thread_id)
    assert second.context.conversation is not None
    assert second.context.conversation.external_id == "new-runtime-session"
    assert len(agent.calls) == 2
    assert not app.workspaces.workspaces_dir.exists()


async def test_trunkline_composer_needs_no_existing_profile(
    app: Octomate, user: User
) -> None:
    app.connect(
        TrunklineTentacle("web", app, config=TrunklineChannelConfig(agents=["inkling"]))
    )
    address = ChannelAddress("web", "thread", str(user.id), str(user.id), "draft")
    result = await discover(
        app=app,
        user=user,
        agent_id="inkling",
        address=address,
    )
    assert result.status == "ready"
    assert await app.users.profile("web", str(user.id)) is None


async def test_refresh_and_no_match_preserve_catalog_state(
    app: Octomate, user: User, agent: DiscoveringAgent, address: ChannelAddress
) -> None:
    await discover(
        app=app,
        user=user,
        agent_id=agent.id,
        address=address,
    )
    agent.descriptors = {
        CommandDescriptor(
            id="new", name="new", description="New", argument_hint="<text>"
        )
    }
    result = await discover(
        app=app,
        user=user,
        agent_id=agent.id,
        address=address,
        refresh=True,
        prefix="absent",
    )
    assert result.status == "ready"
    assert not result.descriptors
    full = await discover(
        app=app,
        user=user,
        agent_id=agent.id,
        address=address,
    )
    assert full.descriptors == agent.descriptors
    assert len(agent.calls) == 2


async def test_session_changes_are_resolved_on_next_lookup(
    app: Octomate,
    user: User,
    agent: DiscoveringAgent,
    address: ChannelAddress,
    conversation: Conversation,
) -> None:
    agent.release = asyncio.Event()
    task = asyncio.create_task(
        discover(
            app=app,
            user=user,
            agent_id=agent.id,
            address=address,
            conversation_id=conversation.id,
        )
    )
    await agent.entered.wait()
    async with async_session() as session:
        loaded = await session.get(Conversation, conversation.id)
        assert loaded is not None
        loaded.external_id = "replacement-session"
        await session.commit()
    agent.release.set()
    result = await task
    assert result.status == "ready"
    assert result.context.conversation is not None
    assert result.context.conversation.external_id is None
    refreshed = await discover(
        app=app,
        user=user,
        agent_id=agent.id,
        address=address,
        conversation_id=conversation.id,
    )
    assert refreshed.status == "ready"
    assert refreshed.context.conversation is not None
    assert refreshed.context.conversation.external_id == "replacement-session"
    assert len(agent.calls) == 2


@pytest.mark.parametrize(
    "address", [ChannelAddress("im", "group", "group", "alice", shared=True)]
)
async def test_subthread_uses_surface_model_but_its_own_workspace(
    app: Octomate,
    user: User,
    agent: DiscoveringAgent,
    address: ChannelAddress,
    conversation: Conversation,
) -> None:
    parent = await app.threads.get(conversation.thread_id)
    assert parent is not None
    child = await app.threads.open_sub_thread(parent)
    child_conversation = await app.conversations.ensure(
        child.id, agent_tentacle_id=agent.id
    )
    result = await discover(
        app=app,
        user=user,
        agent_id=agent.id,
        address=address,
        conversation_id=child_conversation.id,
    )
    assert result.status == "ready"
    assert result.context.cwd == app.workspaces.open(child.id, None).path
    assert result.context.conversation is not None
    assert result.context.conversation.id == child_conversation.id


async def test_existing_conversations_use_stored_model_and_permissions(
    app: Octomate,
    user: User,
    agent: DiscoveringAgent,
    address: ChannelAddress,
    conversation: Conversation,
) -> None:
    agent.permission_modes = (PermissionMode(value="plan", name="Plan"),)
    await discover(
        app=app,
        user=user,
        agent_id=agent.id,
        address=address,
        conversation_id=conversation.id,
    )
    await app.conversations.set_permission_mode(conversation, "plan")
    result = await discover(
        app=app,
        user=user,
        agent_id=agent.id,
        address=address,
        conversation_id=conversation.id,
    )
    assert result.context.permission_mode == "plan"
    assert len(agent.calls) == 2
    stored = await discover(
        app=app,
        user=user,
        agent_id=agent.id,
        address=address,
        conversation_id=conversation.id,
        model="invented",
        permission_mode="invented",
    )
    assert stored.context.model == "test"
    assert stored.context.permission_mode == "plan"
    assert len(agent.calls) == 2
    with pytest.raises(HTTPException, match="modes"):
        await discover(
            app=app,
            user=user,
            agent_id=agent.id,
            address=address,
            permission_mode="invented",
        )


@pytest.fixture
def command_body(
    agent: ExecutingAgent, address: ChannelAddress, conversation: Conversation
) -> dict[str, JsonValue]:
    return {
        "agent_id": agent.id,
        "address": asdict(address),
        "conversation_id": str(conversation.id),
        "command_id": "skill",
        "delivery_id": "command-1",
        "arguments": '  "raw argument"\n--flag=✓  ',
    }


@pytest.mark.parametrize("address", ["im", "trunkline"], indirect=True)
async def test_http_execution_preserves_arguments_and_replays_saved_outcome(
    app: Octomate,
    agent: ExecutingAgent,
    command_body: dict[str, JsonValue],
    address: ChannelAddress,
) -> None:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post(
            "/api/commands/execute",
            headers={"X-Octomate-Request": "1"},
            json=command_body,
        )
        replay = await client.post(
            "/api/commands/execute",
            headers={"X-Octomate-Request": "1"},
            json=command_body,
        )
    assert response.headers["cache-control"] == "no-store"
    segments: list[MessageSegment] = [TextSegment(data={"text": "Done"})]
    outcome = CommandOutcomeEvent(outcome=CommandResult(segments=segments))
    expected: list[CommandStreamEvent] = [outcome]
    if address.channel_tentacle_id == "trunkline":
        expected.insert(0, MessageSentEvent(segments=segments))
    assert command_events(response) == expected
    assert command_events(replay) == command_events(response)
    assert agent.invocations == [
        CommandInvocation(command_id="skill", arguments='  "raw argument"\n--flag=✓  ')
    ]
    assert not app.gateway.sessions
    assert not agent.turns
    assert not agent.streams


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("command_id", ""),
        ("delivery_id", ""),
        ("attachments", [{"type": "file", "data": {"file": "/private/input"}}]),
    ],
)
async def test_http_execution_rejects_invalid_intent_before_dispatch(
    app: Octomate,
    agent: ExecutingAgent,
    command_body: dict[str, JsonValue],
    field: str,
    value: JsonValue,
) -> None:
    command_body[field] = value
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post(
            "/api/commands/execute",
            headers={"X-Octomate-Request": "1"},
            json=command_body,
        )
    assert response.status_code == 422
    assert response.headers["content-type"] == "application/json"
    assert not agent.invocations


@pytest.mark.parametrize("refusal", ["unknown", "busy", "composer", "other_user"])
@pytest.mark.parametrize("address", ["im", "trunkline"], indirect=True)
async def test_http_execution_uses_manager_refusals(
    app: Octomate,
    agent: ExecutingAgent,
    conversation: Conversation,
    command_body: dict[str, JsonValue],
    refusal: str,
    address: ChannelAddress,
) -> None:
    if refusal == "unknown":
        command_body["command_id"] = "removed"
    elif refusal == "busy":
        app.gateway.sessions[conversation.id] = None
    elif refusal == "composer":
        command_body.pop("conversation_id")
    else:
        app.dependency_overrides[current_user] = lambda: User(username="other")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post(
            "/api/commands/execute",
            headers={"X-Octomate-Request": "1"},
            json=command_body,
        )
    events = command_events(response)
    outcome = events[-1]
    assert isinstance(outcome, CommandOutcomeEvent)
    assert isinstance(outcome.outcome, CommandError)
    assert outcome.outcome.status == {
        "composer": "unavailable",
        "other_user": "unavailable",
    }.get(refusal, refusal)
    expected: list[CommandStreamEvent] = [outcome]
    if address.channel_tentacle_id == "trunkline":
        expected.insert(
            0,
            MessageSentEvent(
                segments=[TextSegment(data={"text": outcome.outcome.message})]
            ),
        )
    assert events == expected
    assert not agent.invocations
    async with async_session() as session:
        assert await session.count(ThreadCommand) == 0


@pytest.mark.parametrize("address", ["im", "trunkline"], indirect=True)
async def test_http_stream_uses_native_wire_events_and_replays_only_completion(
    app: Octomate,
    agent: ExecutingAgent,
    conversation: Conversation,
    command_body: dict[str, JsonValue],
    monkeypatch: pytest.MonkeyPatch,
    address: ChannelAddress,
    user: User,
) -> None:
    result = AgentRunResult[ChannelOutput]("Done")

    async def events() -> AsyncGenerator[ReactStreamEvent[ChannelOutput], None]:
        await app.conversations.record_agent_run(
            conversation,
            result.run_id,
            [
                ModelRequest(parts=[UserPromptPart(content="/review")]),
                ModelResponse(parts=[TextPart(content="Done")]),
            ],
        )
        yield MessageSentEvent(segments=[TextSegment(data={"text": "Running"})])
        yield FinalResult("Done", None, None)
        yield AgentRunResultEvent(result)
        assert conversation.id in app.gateway.sessions

    agent.behavior = "stream_complete"
    monkeypatch.setattr(agent, "events", events)
    user_capabilities = AsyncMock(return_value=[])
    monkeypatch.setattr(agent, "user_capabilities", user_capabilities)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post(
            "/api/commands/execute",
            headers={"X-Octomate-Request": "1"},
            json=command_body,
        )
        replay = await client.post(
            "/api/commands/execute",
            headers={"X-Octomate-Request": "1"},
            json=command_body,
        )
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-accel-buffering"] == "no"
    assert "content-length" not in response.headers
    received = command_events(response)
    if address.channel_tentacle_id == "trunkline":
        started = received.pop(0)
        assert isinstance(started, RunStartedEvent)
        assert started.address == address
    assert len(received) == 3
    assert isinstance(received[0], MessageSentEvent)
    assert isinstance(received[1], RunResultEvent)
    assert received[1].output == "Done"
    assert received[-1] == CommandOutcomeEvent(outcome=CommandResult())
    assert command_events(replay) == [received[-1]]
    assert len(agent.invocations) == 1
    assert not app.gateway.sessions
    if address.channel_tentacle_id == "trunkline":
        user_capabilities.assert_awaited_once()
        assert isinstance(agent.suspender, ReflexSuspender)
        gateway = next(
            cap
            for cap in agent.capabilities or []
            if isinstance(cap, GatewayCapability)
        )
        assert gateway.session.user_profile is not None
        assert gateway.session.user_profile.user_id == user.id
        thread = await app.threads.get(conversation.thread_id)
        assert thread is not None
        replies = [
            message for message in thread.messages if message.direction == "outbound"
        ]
        assert len(replies) == 1
        assert replies[0].message_text == "Done"
        bound = await app.threads.related_model_messages(replies[0].id)
        assert len(bound) == 1
        assert bound[0].run_id == result.run_id


@pytest.mark.parametrize("address", ["trunkline"], indirect=True)
async def test_trunkline_command_streams_a_persisted_reflex_approval(
    app: Octomate,
    agent: ExecutingAgent,
    conversation: Conversation,
    address: ChannelAddress,
    command_body: dict[str, JsonValue],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requests = DeferredToolRequests(
        approvals=[ToolCallPart("write_file", {"path": "draft.txt"}, "write")]
    )

    async def events() -> AsyncGenerator[ReactStreamEvent[ChannelOutput], None]:
        assert isinstance(agent.suspender, ReflexSuspender)
        approval = await agent.suspender.suspend(requests)
        assert approval is not None
        yield approval
        yield AgentRunResultEvent(AgentRunResult[ChannelOutput](requests))

    agent.behavior = "stream_complete"
    monkeypatch.setattr(agent, "events", events)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post(
            "/api/commands/execute",
            headers={"X-Octomate-Request": "1"},
            json=command_body,
        )
    received = command_events(response)
    started = received.pop(0)
    assert isinstance(started, RunStartedEvent)
    assert started.address == address
    assert isinstance(received[0], ActionBatchEvent)
    assert len(received[0].approvals) == 1
    batch = await app.deferred_actions.get_batch(uuid.UUID(received[0].batch_id))
    assert batch.conversation_id == conversation.id
    assert batch.run_name == "command"
    assert batch.decision is not None
    assert batch.decision.agent_id == agent.id
    assert batch.decision.model == "test"
    assert received[-1] == CommandOutcomeEvent(outcome=CommandResult())
    assert len(agent.invocations) == 1
    assert not agent.turns
    assert not agent.streams
    assert not app.gateway.sessions


@pytest.mark.parametrize(
    ("stop", "before_first"),
    [
        ("disconnect", False),
        ("send_error", False),
        ("cancel", False),
        ("disconnect", True),
        ("cancel", True),
    ],
)
@pytest.mark.parametrize(
    "address",
    [
        "im",
        pytest.param(
            "trunkline",
            marks=pytest.mark.skip(
                reason="External Reflex cancellation cleanup is deferred."
            ),
        ),
    ],
    indirect=True,
)
async def test_http_stream_interruption_closes_runtime_and_records_failure(
    app: Octomate,
    agent: ExecutingAgent,
    conversation: Conversation,
    command_body: dict[str, JsonValue],
    monkeypatch: pytest.MonkeyPatch,
    stop: str,
    before_first: bool,
) -> None:
    emitted = asyncio.Event()
    closed = asyncio.Event()
    sent: list[Message] = []

    async def events() -> AsyncGenerator[ReactStreamEvent[ChannelOutput], None]:
        owner = asyncio.current_task()
        try:
            if before_first:
                emitted.set()
                await asyncio.Event().wait()
            yield MessageSentEvent(segments=[TextSegment(data={"text": "Running"})])
            await asyncio.Event().wait()
        finally:
            assert asyncio.current_task() is owner
            await asyncio.sleep(0.01)
            assert conversation.id in app.gateway.sessions
            closed.set()

    async def receive() -> Message:
        await emitted.wait()
        if stop != "disconnect":
            await asyncio.Event().wait()
        return {"type": "http.disconnect"}

    async def send(message: Message) -> None:
        sent.append(message)
        if message["type"] == "http.response.body":
            emitted.set()
            if stop == "send_error":
                raise OSError("client disconnected")

    agent.behavior = "stream"
    monkeypatch.setattr(agent, "events", events)
    body = json.dumps(command_body).encode()
    scope: Scope = {
        "type": "http",
        "method": "POST",
        "path": "/api/commands/execute",
        "query_string": b"",
        "headers": [
            (b"content-type", b"application/json"),
            (b"x-octomate-request", b"1"),
        ],
    }
    incoming: asyncio.Queue[Message] = asyncio.Queue()
    incoming.put_nowait({"type": "http.request", "body": body, "more_body": False})

    async def request_receive() -> Message:
        if not incoming.empty():
            return incoming.get_nowait()
        return await receive()

    task = asyncio.create_task(app(scope, request_receive, send))
    async with asyncio.timeout(5):
        await emitted.wait()
        if stop == "cancel":
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        elif stop == "send_error":
            with pytest.raises(ExceptionGroup, match="TaskGroup"):
                await task
        else:
            await task
    assert closed.is_set()
    assert not app.gateway.sessions
    if before_first:
        assert not sent
    assert not any(b"command_outcome" in item.get("body", b"") for item in sent)
    receipt = await app.threads.find_message(
        conversation.thread_id, "command-1", "inbound"
    )
    assert isinstance(receipt, ThreadCommand)
    assert isinstance(receipt.outcome, CommandError)
    assert receipt.outcome.status == "failed"


@pytest.mark.parametrize("address", ["im", "trunkline"], indirect=True)
async def test_http_stream_failure_does_not_report_completion(
    app: Octomate,
    agent: ExecutingAgent,
    conversation: Conversation,
    command_body: dict[str, JsonValue],
) -> None:
    agent.behavior = "stream_raise"
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        with pytest.raises(ExceptionGroup, match="TaskGroup"):
            await client.post(
                "/api/commands/execute",
                headers={"X-Octomate-Request": "1"},
                json=command_body,
            )
    assert agent.stream_closed
    assert not app.gateway.sessions
    receipt = await app.threads.find_message(
        conversation.thread_id, "command-1", "inbound"
    )
    assert isinstance(receipt, ThreadCommand)
    assert isinstance(receipt.outcome, CommandError)
    assert receipt.outcome.status == "failed"


@pytest.mark.parametrize("address", ["im", "trunkline"], indirect=True)
async def test_http_execution_openapi_describes_intent_and_response_types(
    app: Octomate,
    agent: ExecutingAgent,
    command_body: dict[str, JsonValue],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def events() -> AsyncGenerator[ReactStreamEvent[ChannelOutput], None]:
        yield PartStartEvent(index=0, part=TextPart("Running"))
        yield PartDeltaEvent(index=0, delta=TextPartDelta(" command"))
        yield UnknownCustomEvent(name="command_progress", data={"progress": 1})
        yield AgentRunResultEvent(AgentRunResult("Done"))

    monkeypatch.setattr(agent, "events", events)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        schema = (await client.get("/openapi.json")).json()
        agent.behavior = "stream_complete"
        streamed = await client.post(
            "/api/commands/execute",
            headers={"X-Octomate-Request": "1"},
            json=command_body,
        )
        replayed = await client.post(
            "/api/commands/execute",
            headers={"X-Octomate-Request": "1"},
            json=command_body,
        )
        refused = await client.post(
            "/api/commands/execute",
            headers={"X-Octomate-Request": "1"},
            json={**command_body, "delivery_id": "command-2", "command_id": "removed"},
        )
    assert schema["openapi"] == "3.2.0"
    operation = schema["paths"]["/api/commands/execute"]["post"]
    body_schema = operation["requestBody"]["content"]["application/json"]["schema"]
    fields = schema["components"]["schemas"][body_schema["$ref"].rsplit("/", 1)[-1]]
    assert set(fields["required"]) == {
        "agent_id",
        "address",
        "command_id",
        "delivery_id",
    }
    assert fields["properties"]["arguments"]["default"] == ""
    assert set(operation["responses"]["200"]["content"]) == {
        "text/event-stream",
    }
    stream_schema = operation["responses"]["200"]["content"]["text/event-stream"]
    assert "schema" not in stream_schema
    item_schema = stream_schema["itemSchema"]
    data_schema = item_schema["properties"]["data"]
    assert data_schema["type"] == "string"
    assert data_schema["contentMediaType"] == "application/json"
    payload_schema = {
        **data_schema["contentSchema"],
        "components": schema["components"],
    }
    Draft202012Validator.check_schema(payload_schema)
    validator = Draft202012Validator(payload_schema)
    for response in (streamed, replayed, refused):
        for frame in response.text.strip().split("\n\n"):
            data = frame.removeprefix("data: ")
            Draft202012Validator(item_schema).validate({"data": data})
            validator.validate(json.loads(data))
    with pytest.raises(ValidationError):
        validator.validate(
            {"event_kind": "command_outcome", "outcome": {"status": "invented"}}
        )
