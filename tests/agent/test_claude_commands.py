"""Claude's initialization metadata supplies the scoped command catalog.

The fixture retains selected initialize entries from SDK 0.2.152 / CLI 2.1.259.
Capture by connecting without query() in a temporary cwd and CLAUDE_CONFIG_DIR,
with strict_mcp_config and extra_args safe-mode/no-session-persistence enabled.
A .claude/commands/fixture-review.md with description "Review the fixture
workspace" and argument-hint "[target]" appears only when safe-mode is removed,
even with setting_sources=["project"]. No model prompt was sent in either case.
"""

import asyncio
from dataclasses import replace
from pathlib import Path
from types import TracebackType
from unittest.mock import AsyncMock, MagicMock

import pytest
from claude_agent_sdk import ClaudeAgentOptions
from pydantic import TypeAdapter
from uuid_utils.compat import uuid7

from octomate import Octomate
from octomate.config.agents import ClaudeCodeConfig
from octomate.schemas.commands import CommandContext, CommandDescriptor
from octomate.schemas.conversation import ChannelAddress, Conversation
from octomate.tentacles.claude import ClaudeCodeTentacle
from octomate.tentacles.claude import ink as claude_ink
from octomate.tentacles.claude.catalog import ClaudeCommandDescriptor, ClaudeServerInfo
from octomate.types.json import JsonObject


@pytest.fixture
def payload() -> JsonObject:
    fixture = TypeAdapter(JsonObject).validate_json(
        (Path(__file__).parent / "fixtures/claude_initialize_commands.json").read_text()
    )
    info = fixture["initialize"]
    assert isinstance(info, dict)
    return info


@pytest.fixture
def client(payload: JsonObject, monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
    client = AsyncMock()
    client.__aenter__.return_value = client
    client.get_server_info.return_value = payload
    monkeypatch.setattr(claude_ink, "ClaudeSDKClient", MagicMock(return_value=client))
    return client


@pytest.fixture
def agent(client: AsyncMock) -> ClaudeCodeTentacle:
    host = Octomate()
    return ClaudeCodeTentacle(
        "claude",
        host,
        config=ClaudeCodeConfig(),
        commands=host.commands,
        projects=host.projects,
        threads=host.threads,
        files=host.files,
        conversations=host.conversations,
        deferred_actions=host.deferred_actions,
        workspaces=host.workspaces,
        users=host.users,
        bearers=host.bearers,
        mcp=host.mcp,
    )


@pytest.fixture
def context(tmp_path: Path) -> CommandContext:
    return CommandContext(
        agent_id="claude",
        user_id=uuid7(),
        address=ChannelAddress("web", "thread", "chat", "user", "composer"),
        cwd=tmp_path,
        conversation=Conversation(thread_id=uuid7(), agent_tentacle_id="claude"),
        model="selected-model",
        permission_mode="plan",
    )


def test_captured_commands_keep_native_metadata_and_are_hashable(
    payload: JsonObject,
) -> None:
    info = ClaudeServerInfo.model_validate(payload)
    assert info.commands is not None
    descriptors = {command.id: command for command in info.commands}
    assert descriptors["debug"].argument_hint == "[issue description]"
    assert descriptors["context"].argument_hint == ""
    command = descriptors["clear"]
    assert command.name == "clear"
    assert command.aliases == ("reset", "new")
    assert command.accepts_attachments is None
    assert len(set(info.commands)) == 3
    assert (
        ClaudeCommandDescriptor.model_validate_json(command.model_dump_json())
        == command
    )
    assert CommandDescriptor.model_validate(command.model_dump()).name == "clear"
    commands = payload["commands"]
    assert isinstance(commands, list)
    assert command.description == next(
        item["description"]
        for item in commands
        if isinstance(item, dict) and item["name"] == "clear"
    )


@pytest.mark.parametrize("resumed", [False, True])
async def test_probe_uses_context_and_preserves_driven_settings_without_a_query(
    agent: ClaudeCodeTentacle,
    context: CommandContext,
    client: AsyncMock,
    monkeypatch: pytest.MonkeyPatch,
    resumed: bool,
) -> None:
    conversation = context.conversation
    assert conversation is not None
    conversation.external_id = str(uuid7()) if resumed else None

    def create(*, options: ClaudeAgentOptions) -> AsyncMock:
        assert options.cwd == str(context.cwd)
        assert options.model == context.model
        assert options.permission_mode == "plan"
        assert options.strict_mcp_config
        assert options.mcp_servers == {}
        assert options.plugins == []
        assert options.setting_sources is None
        assert options.extra_args == {"safe-mode": None, "no-session-persistence": None}
        assert options.resume == conversation.external_id
        assert bool(options.session_id) is not resumed
        return client

    factory = MagicMock(side_effect=create)
    monkeypatch.setattr(claude_ink, "ClaudeSDKClient", factory)

    async def leave(
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> bool:
        assert agent.driven_sessions
        return False

    client.__aexit__.side_effect = leave
    catalog = await agent.discover_commands(context)
    assert catalog.status == "ready"
    assert len(catalog.descriptors) == 3
    assert catalog.limitations
    assert agent.driven_sessions == {}
    client.query.assert_not_called()
    client.receive_response.assert_not_called()
    assert agent.ink.live_clients == {}
    assert len(factory.call_args_list) == 1


@pytest.mark.parametrize("has_path", [False, True])
async def test_probe_never_prepares_a_missing_workspace(
    agent: ClaudeCodeTentacle,
    context: CommandContext,
    client: AsyncMock,
    has_path: bool,
) -> None:
    assert context.cwd is not None
    missing = context.cwd / "not-created"
    catalog = await agent.discover_commands(
        replace(context, cwd=missing if has_path else None)
    )
    assert catalog.status == "unavailable"
    assert not missing.exists()
    client.__aenter__.assert_not_called()


@pytest.mark.parametrize("commands", [None, [], [{"name": "broken"}]])
async def test_absent_empty_and_malformed_catalogs_are_distinct(
    agent: ClaudeCodeTentacle,
    context: CommandContext,
    client: AsyncMock,
    payload: JsonObject,
    commands: list[JsonObject] | None,
) -> None:
    if commands is None:
        payload.pop("commands")
    else:
        payload["commands"] = [*commands]
    catalog = await agent.discover_commands(context)
    assert catalog.status == (
        "unsupported" if commands is None else "failed" if commands else "ready"
    )
    assert not catalog.descriptors
    client.__aexit__.assert_awaited_once()


async def test_refresh_and_workspace_change_initialize_again(
    agent: ClaudeCodeTentacle,
    context: CommandContext,
    client: AsyncMock,
    payload: JsonObject,
    tmp_path: Path,
) -> None:
    await agent.discover_commands(context)
    payload["commands"] = [
        {"name": "changed", "description": "Changed", "argumentHint": "raw"}
    ]
    assert len((await agent.discover_commands(context)).descriptors) == 3
    refreshed = await agent.discover_commands(context, refresh=True)
    assert {command.name for command in refreshed.descriptors} == {"changed"}
    other = tmp_path / "other"
    other.mkdir()
    payload["commands"] = []
    catalog = await agent.discover_commands(replace(context, cwd=other))
    assert catalog.status == "ready"
    assert not catalog.descriptors
    assert client.__aenter__.await_count == 3
    assert client.__aexit__.await_count == 3
    client.query.assert_not_called()


async def test_runtime_lifecycle_invalidates_cached_commands(
    agent: ClaudeCodeTentacle,
    context: CommandContext,
) -> None:
    await agent.discover_commands(context)
    assert agent.octomate.commands.catalogs
    async with agent:
        assert not agent.octomate.commands.catalogs
        await agent.discover_commands(context)
        assert agent.octomate.commands.catalogs
    assert not agent.octomate.commands.catalogs


async def test_probe_cancellation_closes_client_and_releases_native_claim(
    agent: ClaudeCodeTentacle,
    context: CommandContext,
    client: AsyncMock,
) -> None:
    entered = asyncio.Event()

    async def pending() -> None:
        entered.set()
        await asyncio.Event().wait()

    client.get_server_info.side_effect = pending
    task = asyncio.create_task(agent.discover_commands(context))
    await asyncio.wait_for(entered.wait(), timeout=2)
    await agent.octomate.commands.close()
    with pytest.raises(asyncio.CancelledError):
        await task
    client.__aexit__.assert_awaited_once()
    assert agent.driven_sessions == {}
