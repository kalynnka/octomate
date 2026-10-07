"""Source-derived DSH catalog fixtures exercise discovery without launching dsh."""

import asyncio
from collections.abc import AsyncGenerator, AsyncIterator
from contextlib import suppress
from dataclasses import replace
from functools import partial
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from octomate_protocol.deepseek import ErrResult, OkResult, RpcError, RpcResult
from pydantic import TypeAdapter
from uuid_utils.compat import uuid7

from octomate import Octomate
from octomate.config.agents import DeepseekConfig
from octomate.schemas.commands import CommandContext
from octomate.schemas.conversation import ChannelAddress, Conversation
from octomate.tentacles.deepseek import DeepseekTentacle
from octomate.tentacles.deepseek.catalog import DeepseekCommandDescriptor
from octomate.tentacles.deepseek.client import DeepseekApiClient
from octomate.tentacles.deepseek.wire import MuxFrame, RemoteNotification
from octomate.types.json import JsonObject
from tests.agent.test_deepseek_tentacle import FakeDeepseekApi, patch_gateway


@pytest.fixture
def payload() -> JsonObject:
    return TypeAdapter(JsonObject).validate_json(
        (Path(__file__).parent / "fixtures/deepseek_commands.json").read_text()
    )


@pytest.fixture
def client(payload: JsonObject) -> AsyncMock:
    client = AsyncMock(spec=DeepseekApiClient)
    client.remote.return_value = OkResult(value=payload["commands"])
    return client


@pytest.fixture
async def agent(client: AsyncMock) -> AsyncGenerator[DeepseekTentacle]:
    host = Octomate()
    agent = DeepseekTentacle("deepseek", host, config=DeepseekConfig())
    await agent.ink.client.http_client.aclose()
    agent.ink.client = client

    async def connected() -> None:
        await asyncio.Event().wait()

    task = asyncio.create_task(connected())
    agent.ink.mux_task = task
    try:
        yield agent
    finally:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task
        await agent.commands.close()


@pytest.fixture
def context() -> CommandContext:
    return CommandContext(
        agent_id="deepseek",
        user_id=uuid7(),
        address=ChannelAddress("web", "thread", "chat", "user", "topic"),
        cwd=None,
        conversation=Conversation(
            thread_id=uuid7(),
            agent_tentacle_id="deepseek",
            external_id="session-1",
        ),
    )


async def test_catalog_preserves_native_metadata_without_prompting(
    agent: DeepseekTentacle, client: AsyncMock, context: CommandContext
) -> None:
    catalog = await agent.discover_commands(context)
    assert catalog.status == "ready"
    descriptors = {entry.id: entry for entry in catalog.descriptors}
    assert set(descriptors) == {"compact", "plan", "review", "fork"}
    assert descriptors["fork"].unavailable_reason
    assert descriptors["compact"].argument_hint is None
    assert not descriptors["compact"].accepts_attachments
    assert descriptors["plan"].argument_hint == "[off|message]"
    assert not descriptors["plan"].accepts_attachments
    assert descriptors["review"].argument_hint == "[target]"
    assert not descriptors["review"].accepts_attachments
    plan = descriptors["plan"]
    assert isinstance(plan, DeepseekCommandDescriptor)
    assert plan.definition_id == "@deepseek-ai/dsh-plan-mode"
    assert DeepseekCommandDescriptor.model_validate_json(plan.model_dump_json()) == plan
    assert len({plan, plan.model_copy()}) == 1
    assert any("Goal commands" in limitation for limitation in catalog.limitations)
    client.remote.assert_awaited_once_with("commands/list", {"agentId": "session-1"})
    client.follow.assert_not_called()


@pytest.mark.parametrize("missing", ["conversation", "session", "connection"])
async def test_discovery_does_not_create_a_missing_session_or_connection(
    agent: DeepseekTentacle,
    client: AsyncMock,
    context: CommandContext,
    missing: str,
) -> None:
    assert context.conversation is not None
    if missing == "conversation":
        context = replace(context, conversation=None)
    elif missing == "session":
        context.conversation.external_id = None
    else:
        agent.ink.mux_task = None
    catalog = await agent.discover_commands(context)
    assert catalog.status == "unavailable"
    assert catalog.message
    assert not catalog.descriptors
    client.remote.assert_not_called()


@pytest.mark.parametrize(
    "response",
    [
        ErrResult(error=RpcError(code="connection-refused", message="offline")),
        OkResult(value=None),
        OkResult(value=[{"name": "missing-description"}]),
    ],
)
async def test_failed_discovery_never_reports_an_empty_ready_catalog(
    agent: DeepseekTentacle,
    client: AsyncMock,
    context: CommandContext,
    response: RpcResult,
) -> None:
    client.remote.return_value = response
    catalog = await agent.discover_commands(context)
    assert catalog.status == "failed"
    assert not catalog.descriptors


async def test_empty_registry_keeps_fork_visible_but_disabled(
    agent: DeepseekTentacle, client: AsyncMock, context: CommandContext
) -> None:
    client.remote.return_value = OkResult(value=[])
    catalog = await agent.discover_commands(context)
    assert catalog.status == "ready"
    [descriptor] = catalog.descriptors
    assert descriptor.name == "fork"
    assert "workspace" in (descriptor.unavailable_reason or "")


async def test_registry_cannot_enable_a_fork_without_workspace_relocation(
    agent: DeepseekTentacle, client: AsyncMock, context: CommandContext
) -> None:
    client.remote.return_value = OkResult(
        value=[
            {
                "name": "fork",
                "definitionId": "plugin:fork",
                "description": "Fork the native session",
                "input": {"hint": "[title]"},
            }
        ]
    )
    catalog = await agent.discover_commands(context)
    [descriptor] = catalog.descriptors
    assert isinstance(descriptor, DeepseekCommandDescriptor)
    assert descriptor.definition_id == "plugin:fork"
    assert descriptor.description == "Fork the native session"
    assert descriptor.argument_hint == "[title]"
    assert "workspace" in (descriptor.unavailable_reason or "")


@pytest.mark.parametrize(
    "definition", ["@deepseek-ai/dsh-session-log-export", "plugin:export", None]
)
async def test_export_restriction_follows_plugin_identity(
    agent: DeepseekTentacle,
    client: AsyncMock,
    context: CommandContext,
    definition: str | None,
) -> None:
    client.remote.return_value = OkResult(
        value=[
            {
                "name": "export",
                "definitionId": definition,
                "description": "Export",
                "input": {"attachments": True},
            }
        ]
    )
    catalog = await agent.discover_commands(context)
    descriptor = next(entry for entry in catalog.descriptors if entry.name == "export")
    assert bool(descriptor.unavailable_reason) == (
        definition == "@deepseek-ai/dsh-session-log-export"
    )
    assert not descriptor.accepts_attachments
    client.follow.assert_not_called()


async def test_connection_startup_replaces_unavailable_catalog_and_shutdown_expires_it(
    context: CommandContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    patch_gateway(monkeypatch)
    FakeDeepseekApi.reset()
    FakeDeepseekApi.results["commands/list"] = OkResult(value=[])
    agent = DeepseekTentacle("deepseek", Octomate(), config=DeepseekConfig())
    assert (await agent.discover_commands(context)).status == "unavailable"
    async with agent:
        assert (await agent.discover_commands(context)).status == "ready"
    assert (await agent.discover_commands(context)).status == "unavailable"
    await agent.commands.close()


async def test_cache_keeps_scoped_overrides_and_native_session_changes_separate(
    agent: DeepseekTentacle, client: AsyncMock, context: CommandContext
) -> None:
    first = await agent.discover_commands(context)
    assert (await agent.discover_commands(context)).descriptors == first.descriptors
    client.remote.assert_awaited_once()
    second_context = replace(
        context,
        conversation=Conversation(
            thread_id=uuid7(), agent_tentacle_id="deepseek", external_id="session-2"
        ),
    )
    client.remote.return_value = OkResult(
        value=[
            {
                "name": "plan",
                "description": "Scoped replacement",
                "input": {"hint": "[scope]"},
            }
        ]
    )
    second = await agent.discover_commands(second_context)
    descriptor = next(entry for entry in second.descriptors if entry.name == "plan")
    assert isinstance(descriptor, DeepseekCommandDescriptor)
    assert descriptor.definition_id is None
    assert descriptor.argument_hint == "[scope]"
    assert (await agent.discover_commands(context)).descriptors == first.descriptors
    assert context.conversation is not None
    context.conversation.external_id = "session-3"
    refreshed = await agent.discover_commands(context)
    assert refreshed.descriptors == second.descriptors
    assert [call.args for call in client.remote.await_args_list] == [
        ("commands/list", {"agentId": "session-1"}),
        ("commands/list", {"agentId": "session-2"}),
        ("commands/list", {"agentId": "session-3"}),
    ]


async def test_registry_notifications_invalidate_without_eager_reprobing(
    agent: DeepseekTentacle,
    client: AsyncMock,
    context: CommandContext,
) -> None:
    incoming: asyncio.Queue[RemoteNotification | None] = asyncio.Queue()

    async def frames() -> AsyncIterator[tuple[str, MuxFrame]]:
        while (frame := await incoming.get()) is not None:
            yield "$events", frame
            incoming.task_done()

    client.mux_frames.side_effect = lambda socket: frames()
    await agent.ink.start(
        answer_interaction=agent.answer_interaction,
        invalidate_commands=partial(agent.commands.invalidate, agent_id=agent.id),
    )
    pump = agent.ink.mux_task
    assert pump is not None
    try:
        await agent.discover_commands(context)
        client.remote.return_value = OkResult(value=[])
        for _ in range(2):
            incoming.put_nowait(
                RemoteNotification(type="emit", event="commands/change", args=[])
            )
        await asyncio.wait_for(incoming.join(), 1)
        client.remote.assert_awaited_once()
        changed = await agent.discover_commands(context)
        assert changed.status == "ready"
        assert {entry.name for entry in changed.descriptors} == {"fork"}
        assert client.remote.await_count == 2
        incoming.put_nowait(None)
        await asyncio.wait_for(pump, 1)
        disconnected = await agent.discover_commands(context)
        assert disconnected.status == "unavailable"
        assert client.remote.await_count == 2
    finally:
        pump.cancel()
        with suppress(asyncio.CancelledError):
            await pump
