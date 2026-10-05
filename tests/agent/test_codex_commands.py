"""Codex skill inspection uses SDK messages without starting a model turn."""

import asyncio
from collections.abc import AsyncGenerator
from dataclasses import replace
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from openai_codex.async_client import AsyncCodexClient
from openai_codex.errors import CodexError, MethodNotFoundError, TransportClosedError
from openai_codex.generated.v2_all import (
    ConfigReadResponse,
    ModelListResponse,
    SkillErrorInfo,
    SkillMetadata,
    SkillsChangedNotification,
    SkillsListEntry,
    SkillsListResponse,
)
from openai_codex.models import Notification
from uuid_utils.compat import uuid7

from octomate import Octomate
from octomate.config.agents import CodexConfig
from octomate.schemas.commands import CommandCatalog, CommandContext, CommandDescriptor
from octomate.schemas.conversation import ChannelAddress
from octomate.tentacles.codex import CodexTentacle
from octomate.tentacles.codex import ink as codex_ink
from octomate.tentacles.codex.schemas import CodexCommandDescriptor
from octomate.types.json import JsonObject
from tests.agent.test_model_discovery import codex_model


@pytest.fixture
def context(tmp_path: Path) -> CommandContext:
    return CommandContext(
        agent_id="codex",
        user_id=uuid7(),
        address=ChannelAddress("web", "thread", "chat", "user", "composer"),
        cwd=tmp_path,
        conversation=None,
    )


@pytest.fixture
def inspector(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> tuple[AsyncMock, asyncio.Queue[Notification | CodexError]]:
    client = AsyncMock(spec=AsyncCodexClient)
    notifications: asyncio.Queue[Notification | CodexError] = asyncio.Queue()

    async def receive() -> Notification:
        item = await notifications.get()
        notifications.task_done()
        if isinstance(item, CodexError):
            raise item
        return item

    async def close() -> None:
        notifications.put_nowait(TransportClosedError("closed"))

    client.next_notification.side_effect = receive
    client.close.side_effect = close
    client.request.return_value = SkillsListResponse(
        data=[SkillsListEntry(cwd=str(tmp_path), skills=[], errors=[])]
    )
    runtime = AsyncMock()
    runtime._client = client

    async def enter() -> AsyncMock:
        await client.start()
        await client.initialize()
        return runtime

    runtime.__aenter__.side_effect = enter
    runtime.close = client.close
    monkeypatch.setattr(codex_ink, "SharedCodex", MagicMock(return_value=runtime))
    return client, notifications


@pytest.fixture
async def agent(
    inspector: tuple[AsyncMock, asyncio.Queue[Notification | CodexError]],
) -> AsyncGenerator[CodexTentacle]:
    client, _ = inspector
    host = Octomate()
    tentacle = CodexTentacle(
        "codex",
        host,
        config=CodexConfig(),
        commands=host.commands,
        projects=host.projects,
        threads=host.threads,
        files=host.files,
        conversations=host.conversations,
        deferred_actions=host.deferred_actions,
        workspaces=host.workspaces,
        users=host.users,
        bearers=host.bearers,
        auth=host.auth,
        gateway_manager=host.gateway,
    )
    client.request.side_effect = [
        ConfigReadResponse.model_validate({"config": {}, "origins": {}}),
        ModelListResponse(data=[codex_model("test-model")]),
    ]
    async with tentacle:
        client.request.side_effect = None
        client.request.reset_mock()
        try:
            yield tentacle
        finally:
            await tentacle.octomate.commands.close()


async def test_models_and_skills_share_client(
    context: CommandContext,
    inspector: tuple[AsyncMock, asyncio.Queue[Notification | CodexError]],
) -> None:
    client, _ = inspector
    settings = ConfigReadResponse.model_validate({"config": {}, "origins": {}})
    models = ModelListResponse(data=[codex_model("test-model")])
    client.request.side_effect = [
        settings,
        models,
        client.request.return_value,
        settings,
        models,
    ]
    host = Octomate()
    agent = CodexTentacle(
        "codex",
        host,
        config=CodexConfig(),
        commands=host.commands,
        projects=host.projects,
        threads=host.threads,
        files=host.files,
        conversations=host.conversations,
        deferred_actions=host.deferred_actions,
        workspaces=host.workspaces,
        users=host.users,
        bearers=host.bearers,
        auth=host.auth,
        gateway_manager=host.gateway,
    )
    shared = agent.ink.client
    client.start.assert_not_awaited()

    async with agent:
        watcher = agent.ink.notification_task
        catalog = await agent.discover_commands(context)
        await agent.discover_models()
        assert catalog.status == "ready"
        assert agent.models == {"openai:test-model": "test-model"}
        assert agent.ink.notification_task is watcher
        assert agent.ink.client is shared
        assert [call.args[0] for call in client.request.await_args_list] == [
            "config/read",
            "model/list",
            "skills/list",
            "config/read",
            "model/list",
        ]
        client.start.assert_awaited_once()
        client.initialize.assert_awaited_once()
        client.close.assert_not_awaited()

    client.close.assert_awaited_once()
    assert watcher is not None
    assert watcher.done()
    assert agent.ink.client is shared


async def test_native_metadata_and_duplicate_names(
    agent: CodexTentacle,
    context: CommandContext,
    inspector: tuple[AsyncMock, asyncio.Queue[Notification | CodexError]],
) -> None:
    client, _ = inspector
    client.request.return_value = SkillsListResponse.model_validate(
        {
            "data": [
                {
                    "cwd": str(context.cwd),
                    "errors": [],
                    "skills": [
                        {
                            "name": "review",
                            "description": "Native description\nwith details.",
                            "path": f"/{scope}/review/SKILL.md",
                            "scope": scope,
                            "enabled": enabled,
                            "pluginId": "reviewer" if scope == "user" else None,
                        }
                        for scope, enabled in [
                            ("repo", True),
                            ("user", True),
                            ("system", False),
                        ]
                    ],
                }
            ]
        }
    )
    catalog = await agent.discover_commands(context)
    assert catalog.status == "ready"
    assert len(catalog.descriptors) == 2
    assert {item.id for item in catalog.descriptors} == {
        "skill:/repo/review/SKILL.md",
        "skill:/user/review/SKILL.md",
    }
    for item in catalog.descriptors:
        assert isinstance(item, CodexCommandDescriptor)
        assert item.name == "review"
        assert item.description == "Native description\nwith details."
        assert item.path == Path(f"/{item.scope.value}/review/SKILL.md")
        assert item.plugin_id == ("reviewer" if item.scope.value == "user" else None)
        assert item.argument_hint is None
        assert item.accepts_attachments is None
        assert CommandDescriptor.model_validate(item.model_dump()).name == "review"
    assert '"path":"/user/review/SKILL.md"' in catalog.model_dump_json()
    client.request.assert_awaited_once_with(
        "skills/list",
        {"cwds": [str(context.cwd)], "forceReload": True},
        response_model=SkillsListResponse,
    )
    assert context.conversation is None
    assert {call[0] for call in client.mock_calls} <= {
        "start",
        "initialize",
        "request",
        "next_notification",
    }


@pytest.mark.parametrize("missing", [True, False])
async def test_workspace_is_never_created(
    agent: CodexTentacle,
    context: CommandContext,
    inspector: tuple[AsyncMock, asyncio.Queue[Notification | CodexError]],
    missing: bool,
    tmp_path: Path,
) -> None:
    cwd = tmp_path / "not-created"
    catalog = await agent.discover_commands(
        replace(context, cwd=cwd if missing else None)
    )
    assert catalog.status == "unavailable"
    assert not cwd.exists()
    inspector[0].request.assert_not_awaited()
    inspector[0].start.assert_awaited_once()


@pytest.mark.parametrize(
    ("skills", "errors", "status"),
    [(False, False, "ready"), (True, True, "ready"), (False, True, "failed")],
)
async def test_empty_and_partial_discovery_are_distinct(
    agent: CodexTentacle,
    context: CommandContext,
    inspector: tuple[AsyncMock, asyncio.Queue[Notification | CodexError]],
    skills: bool,
    errors: bool,
    status: str,
) -> None:
    client, _ = inspector
    client.request.return_value = SkillsListResponse(
        data=[
            SkillsListEntry(
                cwd=str(context.cwd),
                skills=[
                    SkillMetadata.model_validate(
                        {
                            "name": "review",
                            "description": "Review",
                            "path": "/review/SKILL.md",
                            "scope": "repo",
                            "enabled": True,
                        }
                    )
                ]
                if skills
                else [],
                errors=[
                    SkillErrorInfo(
                        path="/broken/SKILL.md", message="Invalid frontmatter"
                    )
                ]
                if errors
                else [],
            )
        ]
    )
    catalog = await agent.discover_commands(context)
    assert catalog.status == status
    assert bool(catalog.descriptors) == skills
    assert len(catalog.limitations) == (2 if errors else 1)
    if errors:
        assert catalog.limitations[-1] == "/broken/SKILL.md: Invalid frontmatter"


async def test_refresh_bypasses_both_caches(
    agent: CodexTentacle,
    context: CommandContext,
    inspector: tuple[AsyncMock, asyncio.Queue[Notification | CodexError]],
) -> None:
    client, _ = inspector
    await agent.discover_commands(context)
    await agent.discover_commands(context)
    assert client.request.await_count == 1
    await agent.discover_commands(context, refresh=True)
    assert client.request.await_count == 2
    assert all(call.args[1]["forceReload"] for call in client.request.await_args_list)
    client.start.assert_awaited_once()
    client.initialize.assert_awaited_once()


async def test_notification_invalidates_this_agents_catalogs(
    agent: CodexTentacle,
    context: CommandContext,
    inspector: tuple[AsyncMock, asyncio.Queue[Notification | CodexError]],
) -> None:
    client, notifications = inspector
    await agent.discover_commands(context)
    await agent.discover_commands(replace(context, user_id=uuid7()))
    manager = agent.octomate.commands
    other_key = next(iter(manager.catalogs))._replace(agent_id="other")
    manager.catalogs[other_key] = CommandCatalog(
        context=replace(context, agent_id="other"), status="ready"
    )
    notifications.put_nowait(Notification("unrelated", SkillsChangedNotification()))
    await asyncio.wait_for(notifications.join(), 1)
    assert len(manager.catalogs) == 3
    notifications.put_nowait(
        Notification("skills/changed", SkillsChangedNotification())
    )
    await asyncio.wait_for(notifications.join(), 1)
    assert list(manager.catalogs) == [other_key]
    await agent.discover_commands(context)
    assert client.request.await_count == 3


async def test_disconnect_invalidates_without_restarting_the_client(
    agent: CodexTentacle,
    context: CommandContext,
    inspector: tuple[AsyncMock, asyncio.Queue[Notification | CodexError]],
) -> None:
    client, notifications = inspector
    shared = agent.ink.client
    await agent.discover_commands(context)
    client.request.side_effect = TransportClosedError("runtime exited")
    notifications.put_nowait(TransportClosedError("runtime exited"))
    assert agent.ink.notification_task is not None
    await asyncio.wait_for(agent.ink.notification_task, 1)
    assert not agent.octomate.commands.catalogs
    catalog = await agent.discover_commands(context)
    assert catalog.status == "failed"
    assert agent.ink.client is shared
    client.start.assert_awaited_once()
    client.initialize.assert_awaited_once()


@pytest.mark.parametrize(
    "error", [MethodNotFoundError(-32601, "unsupported"), CodexError("private details")]
)
async def test_errors_do_not_become_empty_catalogs(
    agent: CodexTentacle,
    context: CommandContext,
    inspector: tuple[AsyncMock, asyncio.Queue[Notification | CodexError]],
    error: CodexError,
) -> None:
    inspector[0].request.side_effect = error
    catalog = await agent.discover_commands(context)
    assert catalog.status == (
        "unsupported" if isinstance(error, MethodNotFoundError) else "failed"
    )
    assert catalog.message
    assert "private details" not in catalog.message


async def test_shutdown_closes_the_notification_reader(
    agent: CodexTentacle,
    context: CommandContext,
    inspector: tuple[AsyncMock, asyncio.Queue[Notification | CodexError]],
) -> None:
    await agent.discover_commands(context)
    shared = agent.ink.client
    watcher = agent.ink.notification_task
    await asyncio.wait_for(agent.__aexit__(), 1)
    inspector[0].close.assert_awaited_once()
    assert watcher is not None
    assert watcher.done()
    assert not watcher.cancelled()
    assert agent.ink.client is shared
    assert not agent.octomate.commands.catalogs


async def test_changed_notification_discards_an_in_flight_result(
    agent: CodexTentacle,
    context: CommandContext,
    inspector: tuple[AsyncMock, asyncio.Queue[Notification | CodexError]],
) -> None:
    client, notifications = inspector
    entered, release = asyncio.Event(), asyncio.Event()

    async def request(
        _method: str,
        _params: JsonObject | None,
        *,
        response_model: type[SkillsListResponse],
    ) -> SkillsListResponse:
        entered.set()
        await release.wait()
        return SkillsListResponse(
            data=[SkillsListEntry(cwd=str(context.cwd), skills=[], errors=[])]
        )

    client.request.side_effect = request
    discovery = asyncio.create_task(agent.discover_commands(context))
    await asyncio.wait_for(entered.wait(), 1)
    notifications.put_nowait(
        Notification("skills/changed", SkillsChangedNotification())
    )
    await asyncio.wait_for(notifications.join(), 1)
    release.set()
    assert (await discovery).status == "loading"
    assert not agent.octomate.commands.catalogs


async def test_cancelled_startup_is_drained_before_close(
    inspector: tuple[AsyncMock, asyncio.Queue[Notification | CodexError]],
) -> None:
    client, _ = inspector
    entered, release = asyncio.Event(), asyncio.Event()

    async def start() -> None:
        entered.set()
        await release.wait()

    client.start.side_effect = start
    host = Octomate()
    agent = CodexTentacle(
        "codex",
        host,
        config=CodexConfig(),
        commands=host.commands,
        projects=host.projects,
        threads=host.threads,
        files=host.files,
        conversations=host.conversations,
        deferred_actions=host.deferred_actions,
        workspaces=host.workspaces,
        users=host.users,
        bearers=host.bearers,
        auth=host.auth,
        gateway_manager=host.gateway,
    )
    shared = agent.ink.client
    startup = asyncio.create_task(agent.__aenter__())
    await asyncio.wait_for(entered.wait(), 1)
    startup.cancel()
    await asyncio.sleep(0)
    client.close.assert_not_awaited()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await startup
    client.close.assert_awaited_once()
    client.initialize.assert_awaited_once()
    assert agent.ink.client is shared
    assert agent.ink.notification_task is None
