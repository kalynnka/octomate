"""Explicit skills share Codex's driven turn and command receipt paths."""

import asyncio
from collections.abc import AsyncGenerator, AsyncIterator
from contextlib import AsyncExitStack
from dataclasses import replace
from unittest.mock import AsyncMock

import pytest
from openai_codex import SkillInput, TextInput
from openai_codex.api import ApprovalMode, Sandbox
from openai_codex.errors import CodexError
from openai_codex.generated.v2_all import (
    ReasoningEffort,
    SkillsListEntry,
    TurnCompletedNotification,
)
from openai_codex.models import Notification
from pydantic_ai import AgentRunResultEvent
from pydantic_ai.messages import UserPromptPart
from sqlalchemy.ext.asyncio import AsyncEngine

from octomate import Octomate
from octomate.config import ChannelConfig
from octomate.config.agents import CodexConfig
from octomate.schemas.commands import CommandContext, CommandError, CommandInvocation
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
@pytest.mark.parametrize(
    "effort", [None, "high", "none", "max", "ultra", "future-effort"]
)
async def test_runs_apply_saved_effort(
    execution: tuple[CodexTentacle, CommandContext, AsyncMock],
    command: bool,
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
    context = replace(context, model=model)
    await agent.conversations.set_effort(conversation, "high")
    await agent.conversations.set_effort(conversation, effort)
    if command:
        catalog = await agent.discover_commands(context)
        invocation = CommandInvocation(command_id=next(iter(catalog.descriptors)).id)
        async for _ in agent.execute_command(context, invocation):
            pass
    else:
        await agent.run(
            "review",
            conversation_address=context.address,
            conversation_id=conversation.id,
            thread_id=conversation.thread_id,
            model=model,
        )
    assert FakeCodex.turn_calls[-1].effort == (
        ReasoningEffort(effort) if effort is not None else None
    )


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
    invocation = CommandInvocation(command_id=next(iter(catalog.descriptors)).id)
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
    descriptor = next(iter(catalog.descriptors))
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
    descriptor = next(iter(catalog.descriptors))
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
    descriptor = next(iter(catalog.descriptors))
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
