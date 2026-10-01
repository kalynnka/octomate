"""Native command results captured from SDK 0.2.152 / CLI 2.1.259.

The fixture uses a temporary cwd and CLAUDE_CONFIG_DIR, strict MCP config, and a
loopback fake Anthropic endpoint. Direct commands used safe mode. The skill
capture disabled safe mode to load .claude/commands/fixture-review.md with the
instruction "Respond with Fixture reviewed. The requested target is: $ARGUMENTS".
Only relevant wire fields are retained; paths and UUIDs are sanitized.
No real provider or application database was used.
"""

import asyncio
from collections.abc import AsyncGenerator
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from claude_agent_sdk import ClaudeAgentOptions
from claude_agent_sdk._internal.message_parser import parse_message
from claude_agent_sdk.types import Message
from pydantic import TypeAdapter
from pydantic_ai import AgentRunResultEvent
from pydantic_ai.messages import (
    ModelRequest,
    PartDeltaEvent,
    TextPartDelta,
    UserPromptPart,
)
from sqlalchemy.ext.asyncio import AsyncEngine

from octomate import Octomate
from octomate.config import ChannelConfig
from octomate.config.agents import ClaudeCodeConfig
from octomate.schemas.commands import (
    CommandContext,
    CommandError,
    CommandInvocation,
    CommandResult,
)
from octomate.schemas.events import MessageEvent
from octomate.schemas.segments import TextSegment
from octomate.schemas.thread import ThreadCommand
from octomate.schemas.user import UserProfile
from octomate.tentacles.claude import ClaudeCodeTentacle
from octomate.tentacles.claude import ink as claude_ink
from octomate.tentacles.claude.adapter import ClaudeRunAccumulator
from octomate.types.json import JsonObject
from tests.agent.test_claude_tentacle import KEY
from tests.support.channels import FakeChannelTentacle
from tests.support.users import a_user


@pytest.fixture
def fixture() -> JsonObject:
    return TypeAdapter(JsonObject).validate_json(
        (Path(__file__).parent / "fixtures/claude_command_results.json").read_text()
    )


@pytest.fixture
def streams(fixture: JsonObject) -> dict[str, list[JsonObject]]:
    return TypeAdapter(dict[str, list[JsonObject]]).validate_python(fixture["streams"])


async def native_messages(messages: list[JsonObject]) -> AsyncGenerator[Message]:
    for raw in messages:
        message = parse_message(raw)
        assert message is not None
        yield message


@pytest.fixture
async def execution(
    monkeypatch: pytest.MonkeyPatch,
    in_memory_engine: AsyncEngine,
    fixture: JsonObject,
) -> AsyncGenerator[
    tuple[ClaudeCodeTentacle, CommandContext, AsyncMock, list[ClaudeAgentOptions]]
]:
    client = AsyncMock()
    client.__aenter__.return_value = client
    client.receive_response = MagicMock()
    client.get_server_info.return_value = fixture["initialize"]
    captured_options: list[ClaudeAgentOptions] = []

    def create(options: ClaudeAgentOptions) -> AsyncMock:
        captured_options.append(options)
        return client

    monkeypatch.setattr(claude_ink, "ClaudeSDKClient", MagicMock(side_effect=create))
    app = Octomate()
    app.connect(
        FakeChannelTentacle(
            octomate=app, config=ChannelConfig(type="fake", agents=["claude"])
        )
    )
    agent = app.connect(
        ClaudeCodeTentacle(
            "claude",
            app,
            config=ClaudeCodeConfig(),
            commands=app.commands,
            projects=app.projects,
            threads=app.threads,
            conversations=app.conversations,
            deferred_actions=app.deferred_actions,
            workspaces=app.workspaces,
            users=app.users,
            bearers=app.bearers,
            mcp=app.mcp,
        )
    )
    await agent.discover_models()
    client.reset_mock()
    captured_options.clear()
    user = await a_user("alice")
    thread = await app.threads.ensure(KEY)
    await app.threads.record_inbound(
        MessageEvent(
            tentacle_id=KEY.channel_tentacle_id,
            chat_type=KEY.chat_type,
            chat_id=KEY.chat_id,
            user_id=KEY.user_id,
            sender=UserProfile(channel_user_id=KEY.user_id, user_id=user.id),
        )
    )
    await app.threads.record_handoff(thread, to_agent_tentacle_id=agent.id)
    conversation = await app.conversations.ensure(thread.id, agent_tentacle_id=agent.id)
    async with app.workspaces.open(thread.id, None) as workspace:
        context = CommandContext(
            agent_id=agent.id,
            user_id=user.id,
            address=KEY,
            cwd=workspace.path,
            conversation=conversation,
            model=agent.resolve_model(None),
            permission_mode=agent.default_permission_mode,
        )
        try:
            yield agent, context, client, captured_options
        finally:
            await app.commands.close()


@pytest.mark.parametrize(
    ("command", "script"),
    [
        ("context", "context"),
        ("clear", "clear"),
        ("compact", "compact_empty"),
        ("compact", "compact"),
    ],
)
async def test_direct_commands_persist_receipts_and_native_session_without_model_runs(
    execution: tuple[
        ClaudeCodeTentacle, CommandContext, AsyncMock, list[ClaudeAgentOptions]
    ],
    streams: dict[str, list[JsonObject]],
    command: str,
    script: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    build_result = MagicMock(side_effect=AssertionError("Direct output is not a run"))
    monkeypatch.setattr(ClaudeRunAccumulator, "build_result", build_result)
    agent, context, client, options = execution
    conversation = context.conversation
    assert conversation is not None
    # An existing history must survive clear/compact; the runtime owns its context.
    await agent.conversations.record_agent_run(
        conversation,
        run_id="previous",
        messages=[ModelRequest(parts=[UserPromptPart(content="Earlier work")])],
        external_id="previous-session",
    )
    await agent.conversations.set_external_id(conversation, "previous-session")
    client.receive_response.side_effect = lambda: native_messages(streams[script])
    invocation = CommandInvocation(command_id=command)
    async with agent.commands.execute(
        agent, context, invocation, delivery_id="direct"
    ) as outcome:
        assert isinstance(outcome, CommandResult)
    expected = streams[script][-1]["result"]
    assert [
        segment.data["text"]
        for segment in outcome.segments
        if isinstance(segment, TextSegment)
    ] == ([expected] if expected else [])
    client.query.assert_awaited_once_with(f"/{command}")
    assert options[-1].resume == "previous-session"
    assert options[-1].cwd == str(context.cwd)
    stored = await agent.conversations.get(conversation.id)
    assert stored.external_id == streams[script][-1]["session_id"]
    assert len(await stored.runs) == 1
    receipt = await agent.threads.find_message(
        conversation.thread_id, "direct", "inbound"
    )
    assert isinstance(receipt, ThreadCommand)
    assert receipt.outcome == outcome
    assert agent.driven_sessions == {}
    assert client.__aenter__.await_count == client.__aexit__.await_count
    build_result.assert_not_called()


@pytest.mark.parametrize("resumed", [False, True])
@pytest.mark.parametrize("arguments", ["", '  "raw $HOME"\n--detail=✓  '])
async def test_model_command_preserves_arguments_context_stream_and_history(
    execution: tuple[
        ClaudeCodeTentacle, CommandContext, AsyncMock, list[ClaudeAgentOptions]
    ],
    streams: dict[str, list[JsonObject]],
    resumed: bool,
    arguments: str,
) -> None:
    agent, context, client, options = execution
    conversation = context.conversation
    assert conversation is not None
    if resumed:
        await agent.conversations.set_external_id(conversation, "prior-session")
    client.receive_response.side_effect = lambda: native_messages(streams["skill"])
    invocation = CommandInvocation(command_id="fixture-review", arguments=arguments)
    async with agent.commands.execute(
        agent, context, invocation, delivery_id="model"
    ) as stream:
        assert isinstance(stream, AsyncGenerator)
        events = [event async for event in stream]
    assert isinstance(events[-1], AgentRunResultEvent)
    assert events[-1].result.output == "Fixture reviewed."
    assert [
        e.delta.content_delta
        for e in events
        if isinstance(e, PartDeltaEvent) and isinstance(e.delta, TextPartDelta)
    ] == ["Fixture reviewed."]
    prompt = "/fixture-review" + (f" {arguments}" if arguments else "")
    client.query.assert_awaited_once_with(prompt)
    assert options[-1].resume == ("prior-session" if resumed else None)
    assert options[-1].cwd == str(context.cwd)
    assert options[-1].permission_mode == context.permission_mode
    assert options[-1].extra_args == {"safe-mode": None}
    assert client.get_server_info.await_count == 2
    stored = await agent.conversations.get(conversation.id)
    [run] = await stored.runs
    assert run.native_session_id == streams["skill"][-1]["session_id"]
    assert any(
        isinstance(part, UserPromptPart) and part.content == prompt
        for message in await run.messages
        for part in message.parts
    )
    receipt = await agent.threads.find_message(
        conversation.thread_id, "model", "inbound"
    )
    assert isinstance(receipt, ThreadCommand)
    assert receipt.outcome == CommandResult()
    assert client.__aenter__.await_count == client.__aexit__.await_count


@pytest.mark.parametrize(
    "change",
    [
        "removed_before_discovery",
        "removed_before_query",
        "metadata_changed",
        "terminal_only",
    ],
)
async def test_unknown_or_changed_commands_never_reach_query(
    execution: tuple[
        ClaudeCodeTentacle, CommandContext, AsyncMock, list[ClaudeAgentOptions]
    ],
    change: str,
) -> None:
    agent, context, client, _ = execution
    initial = TypeAdapter(JsonObject).validate_python(
        client.get_server_info.return_value
    )
    changed = {**initial, "commands": []}
    command_id = "context"
    if change == "removed_before_discovery":
        client.get_server_info.return_value = changed
    elif change == "terminal_only":
        command_id = "theme"
    else:
        if change == "metadata_changed":
            changed["commands"] = [
                {"name": "context", "description": "Changed meaning"}
            ]
        client.get_server_info.side_effect = [initial, changed]
    async with agent.commands.execute(
        agent, context, CommandInvocation(command_id=command_id), delivery_id="stale"
    ) as outcome:
        assert isinstance(outcome, CommandError)
        assert outcome.status == (
            "unknown"
            if change in ("removed_before_discovery", "terminal_only")
            else "stale"
        )
    client.query.assert_not_awaited()
    assert agent.driven_sessions == {}
    assert client.__aenter__.await_count == client.__aexit__.await_count


@pytest.mark.parametrize("consume", [False, True])
async def test_abandoned_stream_closes_the_already_started_sdk_client(
    execution: tuple[
        ClaudeCodeTentacle, CommandContext, AsyncMock, list[ClaudeAgentOptions]
    ],
    streams: dict[str, list[JsonObject]],
    consume: bool,
) -> None:
    agent, context, client, _ = execution
    client.receive_response.side_effect = lambda: native_messages(streams["skill"])
    async with agent.commands.execute(
        agent,
        context,
        CommandInvocation(command_id="fixture-review"),
        delivery_id="abandoned",
    ) as stream:
        assert isinstance(stream, AsyncGenerator)
        if consume:
            await anext(stream)
    assert agent.driven_sessions == {}
    assert not agent.octomate.gateway.sessions
    assert client.__aenter__.await_count == client.__aexit__.await_count
    assert context.conversation is not None
    receipt = await agent.threads.find_message(
        context.conversation.thread_id, "abandoned", "inbound"
    )
    assert isinstance(receipt, ThreadCommand)
    assert isinstance(receipt.outcome, CommandError)


async def test_cancelling_before_first_model_event_closes_client(
    execution: tuple[
        ClaudeCodeTentacle, CommandContext, AsyncMock, list[ClaudeAgentOptions]
    ],
) -> None:
    agent, context, client, _ = execution
    submitted = asyncio.Event()

    async def pending() -> AsyncGenerator[Message]:
        submitted.set()
        await asyncio.Event().wait()
        async for message in native_messages([]):
            yield message

    client.receive_response.side_effect = pending

    async def execute() -> None:
        async with agent.commands.execute(
            agent,
            context,
            CommandInvocation(command_id="context"),
            delivery_id="cancelled",
        ):
            pytest.fail("The command should remain pending")

    task = asyncio.create_task(execute())
    await asyncio.wait_for(submitted.wait(), 5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert agent.driven_sessions == {}
    assert not agent.octomate.gateway.sessions
    assert client.__aenter__.await_count == client.__aexit__.await_count


@pytest.mark.parametrize("model_run", [False, True])
async def test_native_error_results_fail_receipts_and_keep_actual_model_history(
    execution: tuple[
        ClaudeCodeTentacle, CommandContext, AsyncMock, list[ClaudeAgentOptions]
    ],
    streams: dict[str, list[JsonObject]],
    model_run: bool,
) -> None:
    agent, context, client, _ = execution
    messages = streams["skill" if model_run else "context"]
    messages[-1].update(
        is_error=True,
        subtype="error_max_turns",
        result=None,
        errors=["Turn limit reached"],
    )
    client.receive_response.side_effect = lambda: native_messages(messages)
    async with agent.commands.execute(
        agent,
        context,
        CommandInvocation(command_id="fixture-review" if model_run else "context"),
        delivery_id="failed",
    ) as outcome:
        if model_run:
            assert isinstance(outcome, AsyncGenerator)
            with pytest.raises(RuntimeError, match="Turn limit reached"):
                _ = [event async for event in outcome]
        else:
            assert isinstance(outcome, CommandError)
            assert outcome.status == "failed"
    assert context.conversation is not None
    stored = await agent.conversations.get(context.conversation.id)
    assert len(await stored.runs) == int(model_run)
    receipt = await agent.threads.find_message(stored.thread_id, "failed", "inbound")
    assert isinstance(receipt, ThreadCommand)
    assert isinstance(receipt.outcome, CommandError)
    assert receipt.outcome.status == "failed"
    assert client.__aenter__.await_count == client.__aexit__.await_count


async def test_missing_terminal_result_never_reports_success(
    execution: tuple[
        ClaudeCodeTentacle, CommandContext, AsyncMock, list[ClaudeAgentOptions]
    ],
    streams: dict[str, list[JsonObject]],
) -> None:
    agent, context, client, _ = execution
    client.receive_response.side_effect = lambda: native_messages(
        streams["context"][:-1]
    )
    async with agent.commands.execute(
        agent,
        context,
        CommandInvocation(command_id="context"),
        delivery_id="incomplete",
    ) as outcome:
        assert isinstance(outcome, CommandError)
        assert outcome.status == "failed"
    assert client.__aenter__.await_count == client.__aexit__.await_count
