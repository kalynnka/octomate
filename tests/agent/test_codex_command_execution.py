"""Explicit skills share Codex's driven turn and command receipt paths."""

import asyncio
from collections.abc import AsyncGenerator, AsyncIterator
from contextlib import AsyncExitStack
from dataclasses import replace
from unittest.mock import AsyncMock

import pytest
from openai_codex import AsyncTurnHandle, SkillInput, TextInput, TurnResult
from openai_codex._run import _collect_async_turn_result
from openai_codex.api import ApprovalMode, Sandbox
from openai_codex.errors import CodexError
from openai_codex.generated.v2_all import (
    ItemCompletedNotification,
    ListMcpServerStatusResponse,
    ReasoningEffort,
    ReviewTarget,
    SkillsListEntry,
    TurnCompletedNotification,
    TurnStatus,
)
from openai_codex.models import Notification
from pydantic_ai import AgentRunResultEvent
from pydantic_ai.messages import ModelRequest, UserPromptPart
from sqlalchemy.ext.asyncio import AsyncEngine
from uuid_utils.compat import uuid7

from octomate import Octomate
from octomate.config import ChannelConfig
from octomate.config.agents import CodexConfig
from octomate.schemas.commands import (
    CommandContext,
    CommandError,
    CommandInvocation,
    CommandResult,
)
from octomate.schemas.events import MessageEvent
from octomate.schemas.thread import ThreadCommand
from octomate.schemas.triage import Claim
from octomate.schemas.user import UserProfile
from octomate.tentacles.codex import CodexTentacle
from octomate.tentacles.codex import ink as codex_ink
from octomate.tentacles.codex.schemas import ThreadSettingsUpdateResponse
from tests.agent.test_codex_tentacle import (
    KEY,
    FakeCodex,
    FakeThread,
    FakeTurn,
    failed_script,
    reset_fake_codex,
    text_script,
)
from tests.support.channels import FakeChannelTentacle
from tests.support.users import a_user


@pytest.fixture
async def execution(
    monkeypatch: pytest.MonkeyPatch, in_memory_engine: AsyncEngine
) -> AsyncGenerator[tuple[CodexTentacle, CommandContext, AsyncMock]]:
    monkeypatch.setattr(codex_ink, "SharedCodex", FakeCodex)
    monkeypatch.setattr(codex_ink, "AsyncThread", lambda client, id: FakeThread(id))
    reset_fake_codex(text_script("reviewed", thread_id="thread-new"))
    app = Octomate()
    app.connect(
        FakeChannelTentacle(
            octomate=app, config=ChannelConfig(type="fake", agents=["codex"])
        )
    )
    agent = app.connect(
        CodexTentacle(
            "codex",
            app,
            config=CodexConfig(permission_mode="auto_review"),
            commands=app.commands,
            projects=app.projects,
            threads=app.threads,
            files=app.files,
            conversations=app.conversations,
            deferred_actions=app.deferred_actions,
            workspaces=app.workspaces,
            users=app.users,
            bearers=app.bearers,
            auth=app.auth,
            gateway_manager=app.gateway,
        )
    )
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
    workspace = app.workspaces.open(thread.id, None)
    async with workspace:
        context = CommandContext(
            agent_id=agent.id,
            user_id=user.id,
            address=KEY,
            cwd=workspace.path,
            conversation=conversation,
            permission_mode=agent.default_permission_mode,
        )
        skills = AsyncMock(
            return_value=SkillsListEntry.model_validate(
                {
                    "cwd": str(workspace.path),
                    "errors": [],
                    "skills": [
                        {
                            "name": "review",
                            "path": str(workspace.path / "review/SKILL.md"),
                            "description": "Review this workspace",
                            "enabled": True,
                            "scope": "repo",
                        }
                    ],
                }
            )
        )
        monkeypatch.setattr(agent.ink, "skills", skills)
        async with agent:
            try:
                yield agent, context, skills
            finally:
                await app.commands.close()


@pytest.mark.parametrize("command", [False, True])
@pytest.mark.parametrize("model_selection", ["qualified", "bare", "default"])
@pytest.mark.parametrize(
    "effort", [None, "high", "none", "max", "ultra", "future-effort"]
)
async def test_runs_apply_saved_effort(
    execution: tuple[CodexTentacle, CommandContext, AsyncMock],
    command: bool,
    model_selection: str,
    effort: str | None,
) -> None:
    agent, context, _ = execution
    conversation = context.conversation
    assert conversation is not None
    model = next(iter(agent.models))
    agent.claims = {
        model: Claim(
            ability="Test model",
            efforts=("high", "none", "max", "ultra", "future-effort"),
        )
    }
    agent.routes = agent.build_routes()
    selected = (
        model
        if model_selection == "qualified"
        else model.partition(":")[2]
        if model_selection == "bare"
        else None
    )
    context = replace(context, model=selected)
    await agent.conversations.set_effort(conversation, "high")
    await agent.conversations.set_effort(conversation, effort)
    if command:
        catalog = await agent.discover_commands(context)
        invocation = CommandInvocation(
            command_id=next(
                item for item in catalog.descriptors if item.id.startswith("skill:")
            ).id
        )
        async for _ in agent.execute_command(context, invocation):
            pass
    else:
        await agent.run(
            "review",
            conversation_address=context.address,
            conversation_id=conversation.id,
            thread_id=conversation.thread_id,
            model=selected,
        )
    assert FakeCodex.turn_calls[-1].effort == (
        ReasoningEffort(effort) if effort is not None else None
    )
    if model_selection == "default":
        assert FakeCodex.turn_calls[-1].model is None
    elif command:
        assert FakeCodex.turn_calls[-1].model == model.partition(":")[2]


async def test_effort_uses_last_reported_model_instead_of_new_session_default(
    execution: tuple[CodexTentacle, CommandContext, AsyncMock],
) -> None:
    agent, context, _ = execution
    conversation = context.conversation
    assert conversation is not None
    agent.set_model_catalog(
        {"openai:test-model": "test-model", "openai:previous": "previous"},
        {
            "openai:test-model": Claim("Default", efforts=("low",)),
            "openai:previous": Claim("Previous", efforts=("high",)),
        },
    )
    await agent.conversations.record_agent_run(
        conversation,
        str(uuid7()),
        [ModelRequest(parts=[UserPromptPart("previous")])],
        model_name="previous",
    )
    await agent.conversations.record_agent_run(
        conversation, str(uuid7()), [ModelRequest(parts=[UserPromptPart("unknown")])]
    )
    await agent.conversations.set_effort(conversation, "high")
    assert await agent.resolve_effort(conversation, model=None) == "high"
    with pytest.raises(ValueError, match="does not take effort 'high'"):
        await agent.resolve_effort(conversation, model="openai:test-model")


async def test_command_rejects_effort_unsupported_by_current_model(
    execution: tuple[CodexTentacle, CommandContext, AsyncMock],
) -> None:
    agent, context, _ = execution
    conversation = context.conversation
    assert conversation is not None
    model = next(iter(agent.models))
    agent.claims = {model: Claim(ability="Test model", efforts=("low",))}
    agent.routes = agent.build_routes()
    context = replace(context, model=model)
    await agent.conversations.set_effort(conversation, "high")
    catalog = await agent.discover_commands(context)
    invocation = CommandInvocation(
        command_id=next(
            item for item in catalog.descriptors if item.id.startswith("skill:")
        ).id
    )
    with pytest.raises(ValueError, match="does not take effort 'high'"):
        async for _ in agent.execute_command(context, invocation):
            pass
    assert not FakeCodex.thread_calls
    assert not FakeCodex.turn_calls


@pytest.mark.parametrize("resumed", [False, True])
@pytest.mark.parametrize("arguments", ["", "  inspect $HOME\n--detail  "])
async def test_skill_turn_preserves_input_context_and_records_outcome(
    execution: tuple[CodexTentacle, CommandContext, AsyncMock],
    resumed: bool,
    arguments: str,
) -> None:
    agent, context, skills = execution
    conversation = context.conversation
    assert conversation is not None
    if resumed:
        await agent.octomate.conversations.set_external_id(conversation, "prior-thread")
        FakeCodex.script = text_script("reviewed", thread_id="prior-thread")
    catalog = await agent.discover_commands(context)
    descriptor = next(
        item for item in catalog.descriptors if item.id.startswith("skill:")
    )
    invocation = CommandInvocation(command_id=descriptor.id, arguments=arguments)
    async with AsyncExitStack() as stack:
        validated = await stack.enter_async_context(
            agent.octomate.commands.validate(
                agent, context, invocation, delivery_id="command-1"
            )
        )
        assert isinstance(validated, tuple)
        stream = await stack.enter_async_context(
            agent.octomate.commands.execute(
                agent, context, invocation, validated, delivery_id="command-1"
            )
        )
        assert isinstance(stream, AsyncGenerator)
        events = [event async for event in stream]
    assert isinstance(events[-1], AgentRunResultEvent)
    assert events[-1].result.output == "reviewed"
    [call] = FakeCodex.turn_calls
    assert context.cwd is not None
    expected = [SkillInput("review", str(context.cwd / "review/SKILL.md"))]
    assert call.prompt == ([*expected, TextInput(arguments)] if arguments else expected)
    assert call.cwd == str(context.cwd)
    assert FakeCodex.thread_calls[0].kind == ("resume" if resumed else "start")
    assert FakeCodex.thread_calls[0].thread_id == ("prior-thread" if resumed else None)
    assert skills.await_count == 2
    skills.assert_awaited_with(context.cwd)
    stored = await agent.octomate.conversations.get(conversation.id)
    [run] = await stored.runs
    assert run.native_session_id == ("prior-thread" if resumed else "thread-new")
    prompt = "/review" + (f" {arguments}" if arguments else "")
    assert any(
        isinstance(part, UserPromptPart) and part.content == prompt
        for message in await run.messages
        for part in message.parts
    )
    receipt = await agent.octomate.threads.find_message(
        conversation.thread_id, "command-1", "inbound"
    )
    assert isinstance(receipt, ThreadCommand)
    assert receipt.invocation == invocation
    assert receipt.outcome is not None
    assert receipt.outcome.status == "completed"


@pytest.mark.parametrize("change", ["disabled", "removed", "other_workspace", "forged"])
async def test_skill_execution_rejects_stale_or_undiscovered_paths(
    execution: tuple[CodexTentacle, CommandContext, AsyncMock], change: str
) -> None:
    agent, context, skills = execution
    catalog = await agent.discover_commands(context)
    descriptor = next(
        item for item in catalog.descriptors if item.id.startswith("skill:")
    )
    entry = skills.return_value
    assert isinstance(entry, SkillsListEntry)
    command_id = descriptor.id
    if change == "disabled":
        entry.skills[0].enabled = False
    elif change == "removed":
        entry.skills.clear()
    elif change == "other_workspace":
        entry.skills[0].path.root = "/different/workspace/review/SKILL.md"
    else:
        command_id = "skill:/client/supplied/SKILL.md"
    async with agent.octomate.commands.validate(
        agent, context, CommandInvocation(command_id=command_id), delivery_id="stale"
    ) as outcome:
        assert isinstance(outcome, CommandError)
        assert outcome.status == "unknown"
    assert FakeCodex.thread_calls == []
    assert FakeCodex.turn_calls == []


@pytest.mark.parametrize("cancel", [False, True])
async def test_detached_skill_stream_drains_before_releasing_the_command_guard(
    execution: tuple[CodexTentacle, CommandContext, AsyncMock],
    monkeypatch: pytest.MonkeyPatch,
    cancel: bool,
) -> None:
    agent, context, _ = execution
    conversation = context.conversation
    assert conversation is not None
    catalog = await agent.discover_commands(context)
    descriptor = next(
        item for item in catalog.descriptors if item.id.startswith("skill:")
    )
    observed, detach, finish = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def gated_stream(turn: FakeTurn) -> AsyncIterator[Notification]:
        yield FakeCodex.script[0]
        await finish.wait()
        for event in FakeCodex.script[1:]:
            yield event

    async def consume() -> None:
        invocation = CommandInvocation(command_id=descriptor.id)
        async with AsyncExitStack() as stack:
            validated = await stack.enter_async_context(
                agent.octomate.commands.validate(
                    agent, context, invocation, delivery_id="detached"
                )
            )
            assert isinstance(validated, tuple)
            stream = await stack.enter_async_context(
                agent.octomate.commands.execute(
                    agent, context, invocation, validated, delivery_id="detached"
                )
            )
            assert isinstance(stream, AsyncGenerator)
            async for _ in stream:
                observed.set()
                await detach.wait()
                break

    monkeypatch.setattr(FakeTurn, "stream", gated_stream)
    task = asyncio.create_task(consume())
    try:
        await asyncio.wait_for(observed.wait(), timeout=5)
        if cancel:
            task.cancel()
        else:
            detach.set()
        await asyncio.sleep(0)
        assert not task.done()
        assert conversation.id in agent.octomate.gateway.sessions
        assert conversation.id in agent.live_turns
    finally:
        finish.set()
        if cancel:
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(task, timeout=5)
        else:
            await asyncio.wait_for(task, timeout=5)
    assert conversation.id not in agent.octomate.gateway.sessions
    assert agent.live_turns == {}
    assert agent.bridge_contexts == {}
    assert not FakeCodex.turns[0].interrupted
    stored = await agent.octomate.conversations.get(conversation.id)
    assert len(await stored.runs) == 1
    receipt = await agent.octomate.threads.find_message(
        conversation.thread_id, "detached", "inbound"
    )
    assert isinstance(receipt, ThreadCommand)
    assert receipt.outcome is not None
    assert receipt.outcome.status == "failed"


async def test_permission_update_applies_to_next_turn_on_the_same_thread(
    execution: tuple[CodexTentacle, CommandContext, AsyncMock],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agent, context, _ = execution
    conversation = context.conversation
    assert conversation is not None
    finish = asyncio.Event()

    async def stream(turn: FakeTurn) -> AsyncIterator[Notification]:
        for notification in FakeCodex.script:
            if isinstance(notification.payload, TurnCompletedNotification):
                await finish.wait()
            yield notification

    monkeypatch.setattr(FakeTurn, "stream", stream)
    async with agent.run_stream_events(
        "work",
        conversation_address=context.address,
        conversation_id=conversation.id,
        thread_id=conversation.thread_id,
    ) as events:
        try:
            await anext(events)
            assert conversation.id in agent.live_turns
            await agent.set_permission_mode(conversation, "full_access")
            request = agent.ink.client._client.request
            assert isinstance(request, AsyncMock)
            request.assert_awaited_with(
                "thread/settings/update",
                {
                    "threadId": "thread-new",
                    "approvalPolicy": "never",
                    "approvalsReviewer": None,
                    "sandboxPolicy": {"type": "dangerFullAccess"},
                },
                response_model=ThreadSettingsUpdateResponse,
            )
            assert not FakeCodex.turns[0].interrupted
        finally:
            finish.set()
        async for _ in events:
            pass
    assert len(FakeCodex.turn_calls) == 1
    await agent.run(
        "continue",
        conversation_address=context.address,
        conversation_id=conversation.id,
        thread_id=conversation.thread_id,
    )
    stored = await agent.conversations.get(conversation.id)
    assert stored.permission_mode == "full_access"
    assert [run.permission_mode for run in await stored.runs] == [
        "auto_review",
        "full_access",
    ]
    assert len(FakeCodex.thread_calls) == 1
    assert FakeCodex.turn_calls[-1].sandbox is Sandbox.full_access
    assert FakeCodex.turn_calls[-1].approval_mode is ApprovalMode.deny_all


@pytest.mark.parametrize(
    ("mode", "reviewer"),
    [("user_review", "user"), ("auto_review", "auto_review"), (None, "auto_review")],
)
async def test_permission_update_resets_native_reviewer_on_an_idle_thread(
    execution: tuple[CodexTentacle, CommandContext, AsyncMock],
    mode: str | None,
    reviewer: str,
) -> None:
    agent, context, _ = execution
    conversation = context.conversation
    assert conversation is not None
    await agent.set_permission_mode(conversation, "full_access")
    await agent.run(
        "work",
        conversation_address=context.address,
        conversation_id=conversation.id,
        thread_id=conversation.thread_id,
    )
    request = agent.ink.client._client.request
    assert isinstance(request, AsyncMock)
    request.reset_mock()
    await agent.set_permission_mode(conversation, mode)
    request.assert_awaited_once_with(
        "thread/settings/update",
        {
            "threadId": "thread-new",
            "approvalPolicy": "on-request",
            "approvalsReviewer": reviewer,
            "sandboxPolicy": {
                "type": "workspaceWrite",
                "writableRoots": [],
                "networkAccess": False,
                "excludeTmpdirEnvVar": False,
                "excludeSlashTmp": False,
            },
        },
        response_model=ThreadSettingsUpdateResponse,
    )
    assert len(FakeCodex.turn_calls) == 1
    stored = await agent.conversations.get(conversation.id)
    assert stored.permission_mode == mode


@pytest.mark.parametrize("native_thread", [False, True])
async def test_unloaded_permission_selection_does_not_open_a_native_thread(
    execution: tuple[CodexTentacle, CommandContext, AsyncMock],
    native_thread: bool,
) -> None:
    agent, context, _ = execution
    conversation = context.conversation
    assert conversation is not None
    if native_thread:
        await agent.conversations.set_external_id(conversation, "unloaded-thread")
    request = agent.ink.client._client.request
    assert isinstance(request, AsyncMock)
    request.reset_mock()
    await agent.set_permission_mode(conversation, "full_access")
    request.assert_not_awaited()
    assert not FakeCodex.thread_calls
    assert not FakeCodex.turn_calls
    stored = await agent.conversations.get(conversation.id)
    assert stored.permission_mode == "full_access"


async def test_rejected_codex_permission_update_does_not_save_the_selection(
    execution: tuple[CodexTentacle, CommandContext, AsyncMock],
) -> None:
    agent, context, _ = execution
    conversation = context.conversation
    assert conversation is not None
    await agent.run(
        "work",
        conversation_address=context.address,
        conversation_id=conversation.id,
        thread_id=conversation.thread_id,
    )
    request = agent.ink.client._client.request
    assert isinstance(request, AsyncMock)
    request.side_effect = CodexError("permission update rejected")
    with pytest.raises(CodexError, match="permission update rejected"):
        await agent.set_permission_mode(conversation, "full_access")
    stored = await agent.conversations.get(conversation.id)
    assert stored.permission_mode is None


async def test_status_before_a_conversation_does_not_start_codex(
    execution: tuple[CodexTentacle, CommandContext, AsyncMock],
) -> None:
    agent, context, _ = execution
    context = replace(context, conversation=None, cwd=None)
    events = [
        event
        async for event in agent.execute_command(
            context, CommandInvocation(command_id="builtin:status")
        )
    ]
    assert len(events) == 1
    assert isinstance(events[0], CommandResult)
    assert "Conversation: not created" in events[0].model_dump_json()
    assert not FakeCodex.thread_calls
    assert not FakeCodex.turn_calls


@pytest.mark.parametrize("argument", ["", "high", "invalid"])
async def test_reasoning_saves_host_effort_without_a_run(
    execution: tuple[CodexTentacle, CommandContext, AsyncMock], argument: str
) -> None:
    agent, context, _ = execution
    conversation = context.conversation
    assert conversation is not None
    model = next(iter(agent.models))
    agent.claims = {model: Claim(ability="Test", efforts=("high",))}
    agent.routes = agent.build_routes()
    await agent.conversations.set_effort(conversation, "high")
    events = [
        event
        async for event in agent.execute_command(
            replace(context, model=model),
            CommandInvocation(command_id="builtin:reasoning", arguments=argument),
        )
    ]
    stored = await agent.conversations.get(conversation.id)
    assert stored.effort == (argument or None if argument != "invalid" else "high")
    assert isinstance(
        events[0], CommandError if argument == "invalid" else CommandResult
    )
    assert not FakeCodex.turn_calls
    assert not FakeCodex.thread_calls
    assert not await stored.runs


@pytest.mark.parametrize(
    ("argument", "mode"), [("", "plan"), ("on", "plan"), ("off", "default")]
)
async def test_plan_uses_native_preset_preserving_model_and_effort(
    execution: tuple[CodexTentacle, CommandContext, AsyncMock], argument: str, mode: str
) -> None:
    agent, context, _ = execution
    conversation = context.conversation
    assert conversation is not None
    model = next(iter(agent.models))
    agent.claims = {model: Claim(ability="Test", efforts=("high",))}
    agent.routes = agent.build_routes()
    await agent.conversations.set_effort(conversation, "high")
    await agent.conversations.set_external_id(conversation, "existing")
    agent.ink.thread_bindings["existing"] = (conversation.id, None)
    request = agent.ink.client._client.request
    assert isinstance(request, AsyncMock)
    request.reset_mock()
    events = [
        event
        async for event in agent.execute_command(
            replace(context, model=model),
            CommandInvocation(command_id="builtin:plan", arguments=argument),
        )
    ]
    assert isinstance(events[0], CommandResult)
    request.assert_awaited_once_with(
        "thread/settings/update",
        {
            "threadId": "existing",
            "collaborationMode": {
                "mode": mode,
                "settings": {
                    "model": "test-model",
                    "reasoning_effort": "high",
                    "developer_instructions": None,
                },
            },
        },
        response_model=ThreadSettingsUpdateResponse,
    )
    assert not FakeCodex.turn_calls
    stored = await agent.conversations.get(conversation.id)
    assert stored.effort == "high"
    assert stored.permission_mode is None
    assert not await stored.runs


@pytest.mark.parametrize("command", ["plan", "mcp"])
async def test_native_controls_are_disabled_without_an_owned_runtime_thread(
    execution: tuple[CodexTentacle, CommandContext, AsyncMock], command: str
) -> None:
    agent, context, _ = execution
    conversation = context.conversation
    assert conversation is not None
    await agent.conversations.set_external_id(conversation, "existing")
    agent.ink.thread_bindings["existing"] = (uuid7(), None)
    invocation = CommandInvocation(command_id=f"builtin:{command}")
    async with agent.commands.validate(
        agent, context, invocation, delivery_id="unbound"
    ) as outcome:
        assert isinstance(outcome, CommandError)
        assert outcome.status == "unavailable"
    assert (
        await agent.threads.find_message(conversation.thread_id, "unbound", "inbound")
        is None
    )
    assert not FakeCodex.thread_calls
    assert not FakeCodex.turn_calls


async def test_mcp_inspection_is_thread_scoped_and_reads_all_pages(
    execution: tuple[CodexTentacle, CommandContext, AsyncMock],
) -> None:
    agent, context, _ = execution
    conversation = context.conversation
    assert conversation is not None
    await agent.conversations.set_external_id(conversation, "existing")
    agent.ink.thread_bindings["existing"] = (conversation.id, None)
    request = agent.ink.client._client.request
    assert isinstance(request, AsyncMock)
    request.reset_mock()
    request.side_effect = [
        ListMcpServerStatusResponse.model_validate(
            {
                "data": [
                    {
                        "name": "gateway",
                        "authStatus": "bearerToken",
                        "tools": {},
                        "resources": [],
                        "resourceTemplates": [],
                    }
                ],
                "nextCursor": "page-2",
            }
        ),
        ListMcpServerStatusResponse(data=[]),
    ]
    events = [
        event
        async for event in agent.execute_command(
            context, CommandInvocation(command_id="builtin:mcp")
        )
    ]
    assert isinstance(events[0], CommandResult)
    assert "gateway: 0 tools" in events[0].model_dump_json()
    assert [call.args[1] for call in request.await_args_list] == [
        {"threadId": "existing", "cursor": None},
        {"threadId": "existing", "cursor": "page-2"},
    ]
    assert not FakeCodex.turn_calls


async def test_init_streams_and_records_one_real_run(
    execution: tuple[CodexTentacle, CommandContext, AsyncMock],
) -> None:
    agent, context, _ = execution
    conversation = context.conversation
    assert conversation is not None
    events = [
        event
        async for event in agent.execute_command(
            context, CommandInvocation(command_id="builtin:init")
        )
    ]
    assert isinstance(events[-1], AgentRunResultEvent)
    [call] = FakeCodex.turn_calls
    assert isinstance(call.prompt, list)
    assert isinstance(call.prompt[0], TextInput)
    assert "AGENTS.md" in call.prompt[0].text
    stored = await agent.conversations.get(conversation.id)
    assert len(await stored.runs) == 1


@pytest.mark.parametrize("command", ["status", "mcp", "plan", "init", "compact"])
async def test_builtin_arguments_are_rejected_without_native_effects(
    execution: tuple[CodexTentacle, CommandContext, AsyncMock], command: str
) -> None:
    agent, context, _ = execution
    conversation = context.conversation
    assert conversation is not None
    await agent.conversations.set_external_id(conversation, "existing")
    agent.ink.thread_bindings["existing"] = (conversation.id, None)
    request = agent.ink.client._client.request
    assert isinstance(request, AsyncMock)
    request.reset_mock()
    events = [
        event
        async for event in agent.execute_command(
            context,
            CommandInvocation(command_id=f"builtin:{command}", arguments="unexpected"),
        )
    ]
    assert isinstance(events[0], CommandError)
    assert events[0].status == "unsupported"
    request.assert_not_awaited()
    assert not FakeCodex.turn_calls


@pytest.mark.parametrize("status", ["completed", "failed", "interrupted"])
async def test_compact_waits_and_records_only_a_command_receipt(
    execution: tuple[CodexTentacle, CommandContext, AsyncMock],
    monkeypatch: pytest.MonkeyPatch,
    status: str,
) -> None:
    agent, context, _ = execution
    conversation = context.conversation
    assert conversation is not None
    await agent.conversations.set_external_id(conversation, "existing")
    agent.ink.thread_bindings["existing"] = (conversation.id, None)
    entered, finish = asyncio.Event(), asyncio.Event()
    reset_fake_codex(failed_script("Compaction failed", thread_id="existing"))
    terminal = FakeCodex.script[0].payload
    assert isinstance(terminal, TurnCompletedNotification)
    terminal.turn.status = TurnStatus(status)
    if status != "failed":
        terminal.turn.error = None

    async def compact() -> TurnResult:
        entered.set()
        await finish.wait()
        return await _collect_async_turn_result(FakeTurn().stream(), turn_id="turn-1")

    handle = AsyncMock(spec=AsyncTurnHandle)
    handle.run.side_effect = compact
    start = AsyncMock(return_value=handle)
    monkeypatch.setattr(agent.ink, "start_command", start)

    async def execute() -> CommandResult | CommandError:
        invocation = CommandInvocation(command_id="builtin:compact")
        async with AsyncExitStack() as stack:
            validated = await stack.enter_async_context(
                agent.commands.validate(
                    agent,
                    context,
                    invocation,
                    delivery_id="compact-1",
                )
            )
            assert isinstance(validated, tuple)
            outcome = await stack.enter_async_context(
                agent.commands.execute(
                    agent,
                    context,
                    invocation,
                    validated,
                    delivery_id="compact-1",
                )
            )
            assert isinstance(outcome, CommandResult | CommandError)
            return outcome

    task = asyncio.create_task(execute())
    try:
        await asyncio.wait_for(entered.wait(), 2)
        assert not task.done()
        receipt = await agent.threads.find_message(
            conversation.thread_id, "compact-1", "inbound"
        )
        assert isinstance(receipt, ThreadCommand)
        assert receipt.outcome is None
    finally:
        finish.set()
    outcome = await asyncio.wait_for(task, 2)
    assert outcome.status == ("completed" if status == "completed" else "failed")
    start.assert_awaited_once_with("existing")
    stored = await agent.conversations.get(conversation.id)
    assert not await stored.runs
    assert not FakeCodex.turn_calls
    receipt = await agent.threads.find_message(
        conversation.thread_id, "compact-1", "inbound"
    )
    assert isinstance(receipt, ThreadCommand)
    assert receipt.outcome == outcome


@pytest.mark.parametrize("branch", ["", "main"])
async def test_review_streams_native_review_text_and_records_a_real_run(
    execution: tuple[CodexTentacle, CommandContext, AsyncMock],
    monkeypatch: pytest.MonkeyPatch,
    branch: str,
) -> None:
    agent, context, _ = execution
    conversation = context.conversation
    assert conversation is not None
    script = text_script("No issues found", thread_id="thread-new")
    script[1] = Notification(
        method="item/completed",
        payload=ItemCompletedNotification.model_validate(
            {
                "item": {
                    "id": "review-1",
                    "type": "exitedReviewMode",
                    "review": "No issues found",
                },
                "threadId": "thread-new",
                "turnId": "turn-1",
                "completedAtMs": 1,
            }
        ),
    )
    reset_fake_codex(script[1:])
    start = AsyncMock(return_value=FakeTurn())
    monkeypatch.setattr(agent.ink, "start_command", start)
    model = next(iter(agent.models))
    agent.claims = {model: Claim(ability="Review", efforts=("medium",))}
    agent.routes = agent.build_routes()
    context = replace(context, model=model)
    await agent.conversations.set_effort(conversation, "medium")
    events = [
        event
        async for event in agent.execute_command(
            context,
            CommandInvocation(command_id="builtin:review", arguments=branch),
        )
    ]
    assert isinstance(events[-1], AgentRunResultEvent)
    assert events[-1].result.output == "No issues found"
    start.assert_awaited_once()
    native_thread, target = start.call_args.args
    assert native_thread == "thread-new"
    assert isinstance(target, ReviewTarget)
    assert target.model_dump() == (
        {"type": "baseBranch", "branch": branch}
        if branch
        else {"type": "uncommittedChanges"}
    )
    assert not FakeCodex.turn_calls
    stored = await agent.conversations.get(conversation.id)
    [run] = await stored.runs
    assert run.native_session_id == "thread-new"
    assert run.native_turn_id == "turn-1"
    assert run.name == "review"
    request = agent.ink.client._client.request
    assert isinstance(request, AsyncMock)
    settings = next(
        call.args[1]
        for call in request.await_args_list
        if call.args[0] == "thread/settings/update"
    )
    assert settings["effort"] == "medium"
    assert settings["model"] == model.partition(":")[2]
    assert settings["approvalsReviewer"] == "auto_review"
    assert settings["sandboxPolicy"]["type"] == "workspaceWrite"


@pytest.mark.parametrize("argument", ["--unknown", "main extra"])
async def test_review_rejects_invalid_targets_before_starting_a_run(
    execution: tuple[CodexTentacle, CommandContext, AsyncMock], argument: str
) -> None:
    agent, context, _ = execution
    events = [
        event
        async for event in agent.execute_command(
            context,
            CommandInvocation(command_id="builtin:review", arguments=argument),
        )
    ]
    assert isinstance(events[0], CommandError)
    assert events[0].status == "unsupported"
    assert not FakeCodex.thread_calls
