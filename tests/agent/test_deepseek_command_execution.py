"""DSH commands retain direct feedback or own one native turn through Reflex."""

import asyncio
from collections.abc import AsyncGenerator
from dataclasses import replace

import pytest
from octomate_protocol.deepseek import ErrResult, OkResult, RpcError, RpcResult
from pydantic_ai.exceptions import AgentRunError
from sqlalchemy.ext.asyncio import AsyncEngine

from octomate import Octomate
from octomate.config import ChannelConfig, ChannelStreamConfig
from octomate.config.agents import DeepseekConfig
from octomate.database import async_session
from octomate.reflex.state import ReflexResult
from octomate.schemas.awakes import CommandSignal
from octomate.schemas.commands import (
    CommandContext,
    CommandError,
    CommandInvocation,
    CommandResult,
)
from octomate.schemas.conversation import ChannelAddress
from octomate.schemas.events import MessageEvent
from octomate.schemas.segments import TextSegment
from octomate.schemas.thread import ThreadCommand
from octomate.schemas.user import UserProfile
from octomate.tentacles.deepseek import DeepseekTentacle
from octomate.tentacles.deepseek.wire import (
    ApprovalRequestedFrame,
    CommandExecutionValue,
    StreamErrorFrame,
)
from octomate.types.json import JsonObject
from tests.agent.test_deepseek_tentacle import (
    FakeDeepseekApi,
    calls_of,
    patch_gateway,
    turn_events,
)
from tests.support.channels import FakeChannelTentacle
from tests.support.managers import RecordingWorkspaceManager
from tests.support.users import a_user

type Scenario = tuple[Octomate, DeepseekTentacle, FakeChannelTentacle, CommandSignal]


@pytest.fixture
async def scenario(
    in_memory_engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> AsyncGenerator[Scenario]:
    patch_gateway(monkeypatch)
    FakeDeepseekApi.reset()
    FakeDeepseekApi.results["commands/list"] = OkResult(
        value=[
            {
                "name": "plan",
                "description": "Enter or leave plan mode",
                "input": {"hint": "[off|message]"},
            }
        ]
    )
    app = Octomate(workspaces=RecordingWorkspaceManager())
    agent = app.connect(DeepseekTentacle("deepseek", app, config=DeepseekConfig()))
    channel = app.connect(
        FakeChannelTentacle(
            octomate=app, config=ChannelConfig(type="fake", agents=[agent.id])
        )
    )
    user = await a_user()
    address = ChannelAddress(channel.id, "thread", str(user.id), str(user.id), "topic")
    thread = await app.threads.ensure(address)
    await app.threads.record_inbound(
        MessageEvent(
            tentacle_id=channel.id,
            chat_type=address.chat_type,
            chat_id=address.chat_id,
            channel_thread_id=address.channel_thread_id,
            user_id=address.user_id,
            sender=UserProfile(channel_user_id=address.user_id, user_id=user.id),
            segments=[TextSegment(data={"text": "Earlier prompt"})],
        )
    )
    conversation = await app.conversations.ensure(thread.id, agent_tentacle_id=agent.id)
    async with async_session() as session:
        saved = await session.get(type(conversation), conversation.id)
        assert saved is not None
        saved.external_id = "sess-1"
        await session.commit()
    conversation.external_id = "sess-1"
    await app.threads.record_handoff(
        thread, to_agent_tentacle_id=agent.id, to_model=None
    )
    signal = CommandSignal(
        CommandContext(
            agent_id=agent.id,
            user_id=user.id,
            address=address,
            cwd=app.workspaces.open(thread.id, None).path,
            conversation=conversation,
            permission_mode=agent.default_permission_mode,
        ),
        CommandInvocation(command_id="plan", arguments=' \t"objective"\n--flag=✓  '),
        "command-1",
    )
    remote = FakeDeepseekApi.remote

    async def execute(
        self: FakeDeepseekApi, endpoint: str, args: JsonObject
    ) -> RpcResult:
        line = args.get("line")
        if (
            endpoint == "commands/execute"
            and isinstance(line, str)
            and line.startswith("/permission ")
        ):
            FakeDeepseekApi.calls.append((endpoint, {"args": args}))
            return OkResult(
                value={"commandId": "permission-1", "result": {"kind": "success"}}
            )
        result = await remote(self, endpoint, args)
        if (
            endpoint != "commands/execute"
            or not isinstance(result, OkResult)
            or result.value is None
        ):
            return result
        execution = CommandExecutionValue.model_validate(result.value)
        FakeDeepseekApi.last_session = "sess-1"
        FakeDeepseekApi.push(
            "sess-1",
            [
                {
                    "type": "command/run",
                    "seq": 0,
                    "time": 0,
                    "data": {"commandId": execution.command_id},
                },
                *FakeDeepseekApi.turn_script,
                {
                    "type": "command/done",
                    "seq": 20,
                    "time": 2,
                    "data": {
                        "commandId": execution.command_id,
                        **execution.result.model_dump(mode="json", by_alias=True),
                    },
                },
            ],
        )
        return result

    monkeypatch.setattr(FakeDeepseekApi, "remote", execute)
    try:
        async with agent:
            yield app, agent, channel, signal
            assert not agent.ink.subscribers
            assert not agent.bridge_contexts
            assert not agent.driven_sessions
    finally:
        await app.commands.close()


@pytest.mark.parametrize(
    "kind", ["success", "silent", "error", "unmatched", "remote_error"]
)
async def test_direct_results_are_recorded_once_without_a_model_run(
    scenario: Scenario, kind: str
) -> None:
    app, _, _, signal = scenario
    result: JsonObject = {"kind": "success", "text": "  Plan mode off.\n"}
    if kind == "silent":
        result = {"kind": "success"}
    elif kind == "error":
        result = {"kind": "error", "text": "Command rejected"}
    FakeDeepseekApi.results["commands/execute"] = (
        ErrResult(error=RpcError(code="offline", message="disconnected"))
        if kind == "remote_error"
        else OkResult(
            value=None
            if kind == "unmatched"
            else {"commandId": "cmd-1", "result": result}
        )
    )
    outcome = await asyncio.wait_for(app.kick(signal), 3)
    assert isinstance(outcome, CommandResult | CommandError)
    assert (
        outcome.status
        == {
            "success": "completed",
            "silent": "completed",
            "error": "failed",
            "unmatched": "stale",
            "remote_error": "failed",
        }[kind]
    )
    if isinstance(outcome, CommandResult):
        assert [str(segment) for segment in outcome.segments] == (
            ["  Plan mode off.\n"] if kind == "success" else []
        )
    assert calls_of("commands/execute") == [
        {
            "args": {
                "agentId": "sess-1",
                "line": "/permission workspace-write",
                "submittedAttachments": [],
            }
        },
        {
            "args": {
                "agentId": "sess-1",
                "line": f"/plan {signal.invocation.arguments}",
                "submittedAttachments": [],
            }
        },
    ]
    assert not calls_of("session/prompt")
    conversation = signal.context.conversation
    assert conversation is not None
    stored = await app.conversations.get(conversation.id)
    assert not await stored.runs
    receipt = await app.threads.find_message(
        conversation.thread_id, signal.delivery_id, "inbound"
    )
    assert isinstance(receipt, ThreadCommand)
    assert receipt.outcome == outcome
    assert await app.kick(signal) == outcome
    assert len(calls_of("commands/execute")) == 2


@pytest.mark.parametrize("stream", [False, True])
async def test_plan_run_uses_the_existing_command_entry_once(
    scenario: Scenario, stream: bool
) -> None:
    app, _, channel, signal = scenario
    channel.config.stream = ChannelStreamConfig(enabled=stream)
    FakeDeepseekApi.results["commands/execute"] = OkResult(
        value={
            "commandId": "cmd-1",
            "result": {"kind": "success", "text": "Plan mode on."},
        }
    )
    native = turn_events("The actual plan")
    message = native[2]
    assert isinstance(message, dict)
    message["data"] = {
        "message": {
            "content": [{"type": "text", "text": "The actual plan"}],
            "source": {
                "kind": "model",
                "provider": "deepseek-official",
                "model": "deepseek-v4-flash",
            },
        }
    }
    native.insert(
        1,
        {
            "type": "user/message",
            "seq": 1,
            "time": 1,
            "data": {
                "content": [{"type": "text", "text": "Design the feature"}],
                "source": {"kind": "user"},
            },
        },
    )
    FakeDeepseekApi.turn_script = native
    result = await asyncio.wait_for(app.kick(signal), 3)
    assert isinstance(result, ReflexResult)
    assert result.result is not None
    assert result.result.output == "The actual plan"
    assert not calls_of("session/prompt")
    conversation = signal.context.conversation
    assert conversation is not None
    stored = await app.conversations.get(conversation.id)
    runs = await stored.runs
    assert len(runs) == 1
    assert runs[0].native_turn_id == "sess-1:1"
    assert runs[0].model_name == "deepseek-v4-flash"
    assert runs[0].permission_mode == signal.context.permission_mode
    messages = await runs[0].messages
    assert len(messages) == 2
    contents = str([message.message_text for message in messages])
    assert "/plan" not in contents
    assert "Design the feature" in contents
    assert "Plan mode on." not in contents
    shown = [
        message.get("text", "")
        for _, _, messages, *_ in channel.sent
        for message in messages
    ]
    assert "Plan mode on." in shown
    assert "The actual plan" in shown
    receipt = await app.threads.find_message(
        conversation.thread_id, signal.delivery_id, "inbound"
    )
    assert isinstance(receipt, ThreadCommand)
    assert receipt.outcome == CommandResult()
    assert await app.kick(signal) == CommandResult()
    assert len(calls_of("commands/execute")) == 2


async def test_stream_failure_preserves_partial_run_and_fails_receipt(
    scenario: Scenario,
) -> None:
    app, _, _, signal = scenario
    FakeDeepseekApi.turn_script = [
        *turn_events("Partial reply")[:-1],
        StreamErrorFrame(
            type="stream/error",
            session_id="sess-1",
            error=RpcError(code="internal", message="socket failed"),
        ),
    ]
    with pytest.raises(AgentRunError, match="socket failed"):
        await asyncio.wait_for(app.kick(signal), 3)
    conversation = signal.context.conversation
    assert conversation is not None
    stored = await app.conversations.get(conversation.id)
    assert len(await stored.runs) == 1
    receipt = await app.threads.find_message(
        conversation.thread_id, signal.delivery_id, "inbound"
    )
    assert isinstance(receipt, ThreadCommand)
    assert isinstance(receipt.outcome, CommandError)


async def test_command_turn_uses_live_approval_bridge(scenario: Scenario) -> None:
    app, _, _, signal = scenario
    conversation = signal.context.conversation
    assert conversation is not None
    conversation.allowed_tools = ["read_file"]
    # Validation reloads the conversation before invoking the native command.
    async with async_session() as session:
        saved = await session.get(type(conversation), conversation.id)
        assert saved is not None
        saved.allowed_tools = ["read_file"]
        await session.commit()
    FakeDeepseekApi.turn_script = [
        turn_events()[0],
        ApprovalRequestedFrame(
            type="approval/requested",
            session_id="sess-1",
            approval_id="approval-1",
            tool_name="read_file",
        ),
    ]
    FakeDeepseekApi.after_respond = turn_events("Approved reply")[1:]
    result = await asyncio.wait_for(app.kick(signal), 3)
    assert isinstance(result, ReflexResult)
    assert len(FakeDeepseekApi.responds) == 1
    assert FakeDeepseekApi.responds[0][1] == OkResult(value="allowed-once")


async def test_permission_change_is_persisted_from_native_event(
    scenario: Scenario,
) -> None:
    app, _, _, signal = scenario
    FakeDeepseekApi.turn_script = [
        {
            "type": "permission/preset",
            "seq": 1,
            "time": 1,
            "data": {"preset": "danger-full-access"},
        }
    ]
    assert isinstance(await asyncio.wait_for(app.kick(signal), 3), CommandResult)
    conversation = signal.context.conversation
    assert conversation is not None
    stored = await app.conversations.get(conversation.id)
    assert stored.permission_mode == "danger-full-access"
    assert not await stored.runs


@pytest.mark.parametrize("arguments", ["", "build it", "pause", "resume", "clear"])
async def test_goal_commands_never_reach_dsh(
    scenario: Scenario, arguments: str
) -> None:
    app, _, _, signal = scenario
    FakeDeepseekApi.results["commands/list"] = OkResult(
        value=[{"name": "goal", "description": "Manage goals"}]
    )
    signal = replace(
        signal, invocation=CommandInvocation(command_id="goal", arguments=arguments)
    )
    outcome = await app.kick(signal)
    assert isinstance(outcome, CommandError)
    assert outcome.status == "unknown"
    assert not calls_of("commands/execute")


async def test_client_owned_export_is_refused_before_dispatch(
    scenario: Scenario,
) -> None:
    app, _, _, signal = scenario
    FakeDeepseekApi.results["commands/list"] = OkResult(
        value=[
            {
                "name": "export",
                "description": "Download logs",
                "definitionId": "@deepseek-ai/dsh-session-log-export",
            }
        ]
    )
    signal = replace(signal, invocation=CommandInvocation(command_id="export"))
    outcome = await app.kick(signal)
    assert isinstance(outcome, CommandError)
    assert outcome.status == "unavailable"
    assert not calls_of("commands/execute")


@pytest.mark.parametrize("saved_effort", [False, True])
async def test_command_run_applies_selected_model_and_permissions(
    scenario: Scenario,
    saved_effort: bool,
) -> None:
    app, agent, _, signal = scenario
    conversation = signal.context.conversation
    assert conversation is not None
    model = "deepseek-official:deepseek-v4-flash"
    thread = await app.threads.get(conversation.thread_id)
    assert thread is not None
    await app.threads.record_handoff(
        thread, to_agent_tentacle_id=agent.id, to_model=model
    )
    await agent.set_permission_mode(conversation, "danger-full-access")
    await agent.conversations.set_effort(conversation, "max" if saved_effort else None)
    assert not calls_of("commands/execute")
    signal = replace(
        signal,
        context=replace(
            signal.context, model=model, permission_mode="danger-full-access"
        ),
    )
    FakeDeepseekApi.turn_script = turn_events("Plan")
    await asyncio.wait_for(app.kick(signal), 3)
    assert calls_of("session/selectModel") == [
        {
            "sessionId": "sess-1",
            "provider": "deepseek-official",
            "model": "deepseek-v4-flash",
            **({"reasoningEffort": "max"} if saved_effort else {}),
        }
    ]
    assert calls_of("commands/execute")[0] == {
        "args": {
            "agentId": "sess-1",
            "line": "/permission danger-full-access",
            "submittedAttachments": [],
        }
    }


@pytest.mark.parametrize("new_session", [False, True])
async def test_dsh_permission_update_reaches_live_run_without_another_prompt(
    scenario: Scenario,
    new_session: bool,
) -> None:
    _, agent, _, signal = scenario
    conversation = signal.context.conversation
    assert conversation is not None
    if new_session:
        async with async_session() as session:
            stored = await session.get(type(conversation), conversation.id)
            assert stored is not None
            stored.external_id = None
            await session.commit()
    FakeDeepseekApi.turn_script = turn_events("Done")[:2]
    async with agent.run_stream_events(
        "work",
        conversation_address=signal.context.address,
        conversation_id=conversation.id,
        thread_id=conversation.thread_id,
    ) as events:
        await anext(events)
        selected = await agent.conversations.get(conversation.id, with_history=False)
        assert selected.external_id == "sess-1"
        await agent.set_permission_mode(selected, "danger-full-access")
        assert calls_of("commands/execute")[-1] == {
            "args": {
                "agentId": "sess-1",
                "line": "/permission danger-full-access",
                "submittedAttachments": [],
            }
        }
        assert len(calls_of("session/prompt")) == 1
        FakeDeepseekApi.push("sess-1", turn_events("Done")[2:])
        async for _ in events:
            pass
    stored = await agent.conversations.get(conversation.id, with_history=False)
    assert stored.permission_mode == "danger-full-access"
    FakeDeepseekApi.turn_script = turn_events("Done")
    await agent.run(
        "continue",
        conversation_address=signal.context.address,
        conversation_id=conversation.id,
        thread_id=conversation.thread_id,
    )
    assert calls_of("commands/execute")[-1] == {
        "args": {
            "agentId": "sess-1",
            "line": "/permission danger-full-access",
            "submittedAttachments": [],
        }
    }
