"""Authorized command discovery without a model turn or new session."""

import asyncio
import uuid
from dataclasses import asdict, replace
from pathlib import Path

import httpx
import pytest
from fastapi import HTTPException
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import AsyncEngine
from uuid_utils.compat import uuid7

from octomate import Octomate
from octomate.auth import current_user
from octomate.commands import command_context, discover_commands
from octomate.config.auth import AuthConfig
from octomate.config.channels import TrunklineChannelConfig
from octomate.database import async_session
from octomate.dependencies import workspace_manager
from octomate.managers.auth import AuthManager
from octomate.managers.workspaces import WorkspaceManager
from octomate.schemas.commands import CommandCatalog, CommandDescriptor
from octomate.schemas.conversation import ChannelAddress, Conversation
from octomate.schemas.events import MessageEvent
from octomate.schemas.thread import Thread
from octomate.schemas.user import User, UserProfile
from octomate.tentacles.trunkline import TrunklineTentacle
from octomate.types.permissions import PermissionMode
from tests.agent.test_command_manager import DiscoveringAgent
from tests.support.channels import FakeChannelTentacle
from tests.support.managers import a_project, a_registry


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
def address() -> ChannelAddress:
    return ChannelAddress("im", "thread", "group", "alice", "topic", shared=True)


@pytest.fixture
def agent() -> DiscoveringAgent:
    return DiscoveringAgent()


@pytest.fixture
async def app(user: User, agent: DiscoveringAgent) -> Octomate:
    app = Octomate()
    app.connect(agent)
    app.connect(FakeChannelTentacle(octomate=app))
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
async def test_http_requires_login_and_browser_header(
    app: Octomate,
    address: ChannelAddress,
    authenticated: bool,
    header: bool,
    status: int,
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
            "/api/commands/catalog",
            headers={"X-Octomate-Request": "1"} if header else {},
            json={"agent_id": "inkling", "address": asdict(address)},
        )
    assert response.status_code == status


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
