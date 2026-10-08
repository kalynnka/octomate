"""Host-owned Claude controls preserve settings and independent session history."""

import json
from dataclasses import replace
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from claude_agent_sdk import ClaudeAgentOptions
from pydantic_ai.messages import ModelRequest, UserPromptPart
from uuid_utils.compat import uuid7

from octomate.schemas.commands import (
    CommandContext,
    CommandError,
    CommandInvocation,
    CommandResult,
)
from octomate.schemas.thread import ThreadCommand
from octomate.tentacles.claude import ClaudeCodeTentacle
from octomate.tentacles.claude.transcript import transcripts_dir
from octomate.types.json import JsonObject
from tests.agent.test_claude_command_execution import (
    execution as execution,
)
from tests.agent.test_claude_command_execution import (
    fixture as fixture,
)
from tests.agent.test_claude_command_execution import (
    native_messages,
)
from tests.agent.test_claude_command_execution import (
    streams as streams,
)


async def deliver(
    agent: ClaudeCodeTentacle,
    context: CommandContext,
    command: str,
    arguments: str = "",
) -> CommandResult | CommandError:
    invocation = CommandInvocation(command_id=command, arguments=arguments)
    async with agent.commands.validate(
        agent, context, invocation, delivery_id="control"
    ) as validated:
        if isinstance(validated, CommandResult | CommandError):
            return validated
        async with agent.commands.execute(
            agent, context, invocation, validated, delivery_id="control"
        ) as result:
            assert isinstance(result, CommandResult | CommandError)
            return result


@pytest.mark.parametrize(
    ("argument", "mode"), [("", "plan"), ("on", "plan"), ("off", "default")]
)
async def test_plan_updates_live_permissions_and_persists_for_next_run(
    execution: tuple[
        ClaudeCodeTentacle, CommandContext, AsyncMock, list[ClaudeAgentOptions]
    ],
    streams: dict[str, list[JsonObject]],
    argument: str,
    mode: str,
) -> None:
    agent, context, client, options = execution
    conversation = context.conversation
    assert conversation is not None
    agent.config.permission_mode = "plan"
    await agent.conversations.set_permission_mode(conversation, "acceptEdits")
    context = replace(context, permission_mode="acceptEdits")
    agent.ink.live_clients[conversation.id] = client
    outcome = await deliver(agent, context, "plan", argument)
    assert isinstance(outcome, CommandResult)
    context = replace(context, permission_mode=mode)
    assert await deliver(agent, context, "plan", argument) == outcome
    client.set_permission_mode.assert_awaited_once_with(mode)
    client.query.assert_not_called()
    stored = await agent.conversations.get(conversation.id)
    assert stored.permission_mode == mode
    assert not await stored.runs
    receipt = await agent.threads.find_message(
        conversation.thread_id, "control", "inbound"
    )
    assert isinstance(receipt, ThreadCommand)
    assert receipt.outcome == outcome
    agent.ink.live_clients.clear()
    client.receive_response.side_effect = lambda: native_messages(streams["skill"])
    await agent.run(
        "continue",
        conversation_address=context.address,
        conversation_id=conversation.id,
        thread_id=conversation.thread_id,
    )
    assert options[-1].permission_mode == mode


@pytest.mark.parametrize(
    ("command", "argument", "status"),
    [
        ("plan", "maybe", "unsupported"),
        ("fork", "other", "unsupported"),
        ("fork", "", "unavailable"),
    ],
)
async def test_controls_reject_invalid_input_without_starting_a_run(
    execution: tuple[
        ClaudeCodeTentacle, CommandContext, AsyncMock, list[ClaudeAgentOptions]
    ],
    command: str,
    argument: str,
    status: str,
) -> None:
    agent, context, client, _ = execution
    outcome = await deliver(agent, context, command, argument)
    assert outcome.status == status
    client.query.assert_not_called()
    client.set_permission_mode.assert_not_called()
    assert context.conversation is not None
    stored = await agent.conversations.get(context.conversation.id)
    assert stored.permission_mode is None
    assert not await stored.runs
    assert len(await agent.threads.list_threads(user_id=context.user_id)) == 1


async def test_fork_copies_sdk_transcript_and_resumes_from_destination(
    execution: tuple[
        ClaudeCodeTentacle, CommandContext, AsyncMock, list[ClaudeAgentOptions]
    ],
    streams: dict[str, list[JsonObject]],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    agent, context, client, options = execution
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude"))
    conversation = context.conversation
    assert conversation is not None
    assert context.cwd is not None
    session_id = str(uuid7())
    prompt_id = str(uuid7())
    source_bytes = (
        json.dumps(
            {
                "type": "user",
                "uuid": prompt_id,
                "parentUuid": None,
                "sessionId": session_id,
                "cwd": str(context.cwd),
                "message": {"role": "user", "content": "Earlier work"},
            }
        )
        + "\n"
    ).encode()
    source_file = transcripts_dir(context.cwd) / f"{session_id}.jsonl"
    source_file.parent.mkdir(parents=True)
    source_file.write_bytes(source_bytes)
    await agent.conversations.record_agent_run(
        conversation,
        run_id="previous",
        external_id=session_id,
        messages=[ModelRequest(parts=[UserPromptPart(content="Earlier work")])],
    )
    await agent.conversations.set_external_id(conversation, session_id)
    await agent.conversations.set_permission_mode(conversation, "plan")
    await agent.conversations.set_effort(conversation, "max")
    context = replace(context, permission_mode="plan")
    source_thread = await agent.threads.get(conversation.thread_id)
    assert source_thread is not None
    await agent.threads.rename(source_thread, "Original")
    outcome = await deliver(agent, context, "fork")
    assert isinstance(outcome, CommandResult)
    assert await deliver(agent, context, "fork") == outcome
    client.query.assert_not_called()
    threads = await agent.threads.list_threads(user_id=context.user_id)
    [target] = [thread for thread in threads if thread.id != source_thread.id]
    assert target.title == "Original"
    copied = await agent.conversations.ensure(target.id, agent_tentacle_id=agent.id)
    assert copied.external_id is not None
    assert copied.external_id != session_id
    copied = await agent.conversations.get(copied.id)
    source = await agent.conversations.get(conversation.id)
    assert source.external_id == session_id
    assert copied.permission_mode == source.permission_mode == "plan"
    assert copied.effort == source.effort == "max"
    assert [message.parts for message in copied.messages] == [
        message.parts for message in source.messages
    ]
    target_cwd = agent.workspaces.open(target.id, None).path
    assert target_cwd != context.cwd
    fork_file = transcripts_dir(target_cwd) / f"{copied.external_id}.jsonl"
    assert fork_file.is_file()
    assert source_file.read_bytes() == source_bytes
    assert not (source_file.parent / fork_file.name).exists()
    assert not (transcripts_dir(target_cwd) / source_file.name).exists()
    entries = [json.loads(line) for line in fork_file.read_text().splitlines()]
    [prompt] = [entry for entry in entries if entry.get("type") == "user"]
    assert prompt["uuid"] != prompt_id
    assert prompt["sessionId"] == copied.external_id
    assert prompt["message"]["content"] == "Earlier work"
    assert len(await source.runs) == 1
    client.receive_response.side_effect = lambda: native_messages(streams["skill"])
    await agent.run(
        "continue the fork",
        conversation_address=replace(
            context.address, channel_thread_id=target.channel_thread_id
        ),
        conversation_id=copied.id,
        thread_id=target.id,
        model=context.model,
    )
    assert options[-1].resume == copied.external_id
    assert options[-1].cwd == str(target_cwd)
    assert options[-1].permission_mode == "plan"
    assert options[-1].effort == "max"
