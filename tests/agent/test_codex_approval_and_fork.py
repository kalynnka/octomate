"""Approval retries and independent forks through real command persistence."""

from dataclasses import replace
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from openai_codex.errors import CodexError
from openai_codex.generated.v2_all import (
    ItemGuardianApprovalReviewCompletedNotification,
    ThreadApproveGuardianDeniedActionResponse,
)
from openai_codex.models import Notification
from pydantic_ai import AgentRunResultEvent
from pydantic_ai.exceptions import AgentRunError

from octomate.config.channels import TrunklineChannelConfig
from octomate.database import async_session
from octomate.schemas.commands import (
    CommandContext,
    CommandError,
    CommandInvocation,
    CommandResult,
)
from octomate.schemas.conversation import ChannelAddress
from octomate.schemas.events import MessageEvent
from octomate.schemas.thread import ThreadCommand
from octomate.schemas.triage import Claim
from octomate.schemas.user import UserProfile
from octomate.tentacles.codex import CodexTentacle
from octomate.tentacles.trunkline import TrunklineTentacle
from octomate.types.json import JsonObject
from tests.agent.test_codex_command_execution import execution as execution
from tests.agent.test_codex_tentacle import FakeCodex, failed_script, text_script
from tests.support.managers import a_project


def review_notification(
    *,
    review_id: str = "review-1",
    thread_id: str = "thread-new",
    turn_id: str = "turn-1",
    status: str = "denied",
    action: JsonObject | None = None,
) -> Notification:
    return Notification(
        method="item/autoApprovalReview/completed",
        payload=ItemGuardianApprovalReviewCompletedNotification.model_validate(
            {
                "action": action
                or {
                    "type": "command",
                    "command": "pytest",
                    "cwd": "/workspace",
                    "source": "unifiedExec",
                },
                "completedAtMs": 2,
                "startedAtMs": 1,
                "decisionSource": "agent",
                "review": {"status": status},
                "reviewId": review_id,
                "threadId": thread_id,
                "turnId": turn_id,
            }
        ),
    )


async def recorded_context(
    agent: CodexTentacle, context: CommandContext
) -> CommandContext:
    assert context.conversation is not None
    await agent.run(
        "Do the work",
        conversation_address=context.address,
        conversation_id=context.conversation.id,
        thread_id=context.conversation.thread_id,
    )
    return replace(
        context,
        conversation=await agent.conversations.get(context.conversation.id),
        cwd=agent.workspaces.open(
            context.conversation.thread_id,
            await agent.run_project(context.conversation.thread_id),
        ).path,
    )


async def deliver(
    agent: CodexTentacle,
    context: CommandContext,
    command: str,
    *,
    arguments: str = "",
    delivery_id: str = "delivery-1",
) -> CommandResult | CommandError:
    invocation = CommandInvocation(command_id=f"builtin:{command}", arguments=arguments)
    async with agent.commands.validate(
        agent, context, invocation, delivery_id=delivery_id
    ) as validated:
        if isinstance(validated, CommandResult | CommandError):
            return validated
        async with agent.commands.execute(
            agent, context, invocation, validated, delivery_id=delivery_id
        ) as result:
            if isinstance(result, CommandResult | CommandError):
                return result
            events = [event async for event in result]
            assert isinstance(events[-1], AgentRunResultEvent)
    assert context.conversation is not None
    receipt = await agent.threads.find_message(
        context.conversation.thread_id, delivery_id, "inbound"
    )
    assert isinstance(receipt, ThreadCommand)
    assert receipt.outcome is not None
    return receipt.outcome


async def test_approve_preserves_late_denial_and_records_one_retry(
    execution: tuple[CodexTentacle, CommandContext, AsyncMock],
) -> None:
    agent, context, _ = execution
    FakeCodex.script.insert(-1, review_notification())
    context = await recorded_context(agent, context)
    FakeCodex.script = text_script("Retried", thread_id="thread-new")
    outcome = await deliver(agent, context, "approve")
    assert outcome.status == "completed"
    request = agent.ink.client._client.request
    assert isinstance(request, AsyncMock)
    approvals = [
        call
        for call in request.await_args_list
        if call.args[0] == "thread/approveGuardianDeniedAction"
    ]
    assert len(approvals) == 1
    assert approvals[0].args[1] == {
        "threadId": "thread-new",
        "event": {
            "id": "review-1",
            "turn_id": "turn-1",
            "status": "denied",
            "action": {
                "type": "command",
                "command": "pytest",
                "cwd": "/workspace",
                "source": "unified_exec",
            },
        },
    }
    assert len(FakeCodex.turn_calls) == 2
    assert context.conversation is not None
    stored = await agent.conversations.get(context.conversation.id)
    assert [run.name for run in stored.runs] == [None, "approve"]
    assert stored.permission_mode is None
    assert await deliver(agent, context, "approve") == outcome
    assert len(FakeCodex.turn_calls) == 2
    refused = await deliver(agent, context, "approve", delivery_id="new-delivery")
    assert isinstance(refused, CommandError)
    assert "latest run" in refused.message


@pytest.mark.parametrize(
    "case", ["wrong-thread", "wrong-turn", "approved", "unknown-id", "multiple"]
)
async def test_approve_refuses_unselected_or_mismatched_reviews(
    execution: tuple[CodexTentacle, CommandContext, AsyncMock],
    case: str,
) -> None:
    agent, context, _ = execution
    FakeCodex.script.insert(
        -1,
        review_notification(
            thread_id="other-thread" if case == "wrong-thread" else "thread-new",
            turn_id="other-turn" if case == "wrong-turn" else "turn-1",
            status="approved" if case == "approved" else "denied",
        ),
    )
    if case == "multiple":
        FakeCodex.script.insert(-1, review_notification(review_id="review-2"))
    context = await recorded_context(agent, context)
    outcome = await deliver(
        agent, context, "approve", arguments="unknown" if case == "unknown-id" else ""
    )
    assert isinstance(outcome, CommandError)
    assert outcome.status == "unavailable"
    assert len(FakeCodex.turn_calls) == 1
    request = agent.ink.client._client.request
    assert isinstance(request, AsyncMock)
    assert not any(
        call.args[0] == "thread/approveGuardianDeniedAction"
        for call in request.await_args_list
    )
    if case == "multiple":
        assert "review-1, review-2" in outcome.message
        FakeCodex.script = text_script("Retried", thread_id="thread-new")
        assert (
            await deliver(
                agent, context, "approve", arguments="review-2", delivery_id="selected"
            )
        ).status == "completed"


@pytest.mark.parametrize(
    ("action", "expected"),
    [
        (
            {
                "type": "networkAccess",
                "target": "proxy",
                "host": "example.com",
                "protocol": "socks5Tcp",
                "port": 443,
            },
            {
                "type": "network_access",
                "target": "proxy",
                "host": "example.com",
                "protocol": "socks5_tcp",
                "port": 443,
            },
        ),
        (
            {
                "type": "mcpToolCall",
                "server": "tools",
                "toolName": "write",
                "connectorId": "connector",
            },
            {
                "type": "mcp_tool_call",
                "server": "tools",
                "tool_name": "write",
                "connector_id": "connector",
            },
        ),
        (
            {
                "type": "requestPermissions",
                "permissions": {"fileSystem": {"write": ["/workspace"]}},
            },
            {
                "type": "request_permissions",
                "permissions": {"file_system": {"write": ["/workspace"]}},
            },
        ),
    ],
)
async def test_approve_translates_native_action_shapes(
    execution: tuple[CodexTentacle, CommandContext, AsyncMock],
    action: JsonObject,
    expected: JsonObject,
) -> None:
    agent, _, _ = execution
    notification = review_notification(action=action)
    assert isinstance(
        notification.payload, ItemGuardianApprovalReviewCompletedNotification
    )
    await agent.ink.approve_denied_action("thread-new", notification.payload)
    request = agent.ink.client._client.request
    assert isinstance(request, AsyncMock)
    request.assert_awaited_with(
        "thread/approveGuardianDeniedAction",
        {
            "threadId": "thread-new",
            "event": {
                "id": "review-1",
                "turn_id": "turn-1",
                "status": "denied",
                "action": expected,
            },
        },
        response_model=ThreadApproveGuardianDeniedActionResponse,
    )


async def test_approve_rpc_failure_does_not_start_a_retry(
    execution: tuple[CodexTentacle, CommandContext, AsyncMock],
) -> None:
    agent, context, _ = execution
    FakeCodex.script.insert(-1, review_notification())
    context = await recorded_context(agent, context)
    request = agent.ink.client._client.request
    assert isinstance(request, AsyncMock)
    request.side_effect = CodexError("approval rejected")
    outcome = await deliver(agent, context, "approve")
    assert outcome.status == "failed"
    assert len(FakeCodex.turn_calls) == 1


@pytest.mark.parametrize("partial_output", [False, True])
async def test_failed_approval_retry_is_recorded_as_a_failed_run(
    execution: tuple[CodexTentacle, CommandContext, AsyncMock],
    partial_output: bool,
) -> None:
    agent, context, _ = execution
    FakeCodex.script.insert(-1, review_notification())
    context = await recorded_context(agent, context)
    FakeCodex.script = failed_script("Retry failed", thread_id="thread-new")
    if partial_output:
        FakeCodex.script = [
            *text_script("Trying", thread_id="thread-new")[:-1],
            *FakeCodex.script,
        ]
        with pytest.raises(AgentRunError, match="Retry failed"):
            await deliver(agent, context, "approve")
    else:
        assert (await deliver(agent, context, "approve")).status == "failed"
    assert context.conversation is not None
    stored = await agent.conversations.get(context.conversation.id)
    assert len(stored.runs) == 2
    assert stored.runs[-1].name == "approve"
    receipt = await agent.threads.find_message(
        stored.thread_id, "delivery-1", "inbound"
    )
    assert isinstance(receipt, ThreadCommand)
    assert receipt.outcome is not None
    assert receipt.outcome.status == "failed"


@pytest.mark.parametrize(
    ("trunkline", "project_bound"), [(False, False), (True, False), (True, True)]
)
async def test_fork_copies_history_settings_and_preserves_source(
    execution: tuple[CodexTentacle, CommandContext, AsyncMock],
    trunkline: bool,
    project_bound: bool,
    tmp_path: Path,
) -> None:
    agent, context, _ = execution
    reported_model = next(iter(agent.models.values()))
    assert isinstance(reported_model, str)
    FakeCodex.model_name = reported_model
    if trunkline:
        app = agent.octomate
        app.connect(
            TrunklineTentacle(
                "trunkline", app, config=TrunklineChannelConfig(agents=[agent.id])
            )
        )
        address = ChannelAddress(
            channel_tentacle_id="trunkline",
            chat_type="thread",
            chat_id=str(context.user_id),
            user_id=str(context.user_id),
            channel_thread_id="source",
        )
        project = None
        if project_bound:
            root = tmp_path / "project"
            root.mkdir()
            (root / "README.md").write_text("Original project")
            project = a_project(root, name="project")
            async with async_session() as session:
                session.add(project)
                await session.commit()
            await agent.projects.load()
        source = await agent.threads.ensure(address, project=project)
        await agent.threads.record_inbound(
            MessageEvent(
                tentacle_id="trunkline",
                chat_type="thread",
                chat_id=address.chat_id,
                channel_thread_id="source",
                user_id=address.user_id,
                sender=UserProfile(
                    channel_user_id=address.user_id, user_id=context.user_id
                ),
            )
        )
        await agent.threads.record_handoff(source, to_agent_tentacle_id=agent.id)
        context = replace(
            context,
            address=address,
            conversation=await agent.conversations.ensure(
                source.id, agent_tentacle_id=agent.id
            ),
        )
    FakeCodex.script.insert(-1, review_notification())
    context = await recorded_context(agent, context)
    assert context.conversation is not None
    original = context.conversation
    if project_bound:
        assert context.cwd is not None
        (context.cwd / "work.txt").write_text("Uncommitted work")
    await agent.conversations.set_effort(original, "high")
    await agent.conversations.set_permission_mode(original, "user_review")
    model = next(iter(agent.models))
    agent.claims = {model: Claim(ability="Test model", efforts=("high",))}
    agent.routes = agent.build_routes()
    context = replace(context, model=model, permission_mode="user_review")
    source_thread = await agent.threads.get(original.thread_id)
    assert source_thread is not None
    await agent.threads.rename(source_thread, "Original conversation")
    await agent.threads.record_handoff(
        source_thread, to_agent_tentacle_id=agent.id, to_model=context.model
    )
    outcome = await deliver(agent, context, "fork")
    assert isinstance(outcome, CommandResult)
    threads = await agent.threads.list_threads(user_id=context.user_id)
    [target] = [
        thread for thread in threads if thread.title == "Fork of Original conversation"
    ]
    target = await agent.threads.get(target.id)
    assert target is not None
    copied = await agent.conversations.ensure(target.id, agent_tentacle_id=agent.id)
    source = await agent.conversations.get(original.id)
    assert source.external_id == "thread-new"
    assert copied.external_id == "thread-fork"
    assert copied.effort == source.effort == "high"
    assert copied.permission_mode == source.permission_mode == "user_review"
    assert [message.parts for message in copied.messages] == [
        message.parts for message in source.messages
    ]
    assert [run.model_name for run in copied.runs] == [
        run.model_name for run in source.runs
    ]
    assert [message.id for message in copied.messages] == [
        message.id for message in source.messages
    ]
    assert target.active_agent_tentacle_id == agent.id
    assert target.active_model == context.model
    assert target.project_id == source_thread.project_id
    if project_bound:
        target_cwd = agent.workspaces.open(target.id, await target.project).path
        assert (target_cwd / "work.txt").read_text() == "Uncommitted work"
        (target_cwd / "work.txt").write_text("Independent fork")
        assert context.cwd is not None
        assert (context.cwd / "work.txt").read_text() == "Uncommitted work"
    with pytest.raises(ValueError, match="latest run"):
        await agent.command_approval(copied, "review-1")
    assert len(FakeCodex.turn_calls) == 1
    assert await deliver(agent, context, "fork") == outcome
    assert len(await agent.threads.list_threads(user_id=context.user_id)) == len(
        threads
    )
    FakeCodex.script = text_script("Continued the fork", thread_id="thread-fork")
    await agent.run(
        "Continue",
        conversation_address=target.key.address(context.address.user_id),
        conversation_id=copied.id,
        thread_id=target.id,
        interactive=False,
    )
    assert FakeCodex.thread_calls[-1].thread_id == "thread-fork"
    assert len((await agent.conversations.get(source.id)).runs) == 1
    assert len((await agent.conversations.get(copied.id)).runs) == 2


async def test_fork_failure_leaves_source_and_publishes_no_destination(
    execution: tuple[CodexTentacle, CommandContext, AsyncMock],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agent, context, _ = execution
    context = await recorded_context(agent, context)
    monkeypatch.setattr(
        agent.ink, "fork_thread", AsyncMock(side_effect=CodexError("fork failed"))
    )
    outcome = await deliver(agent, context, "fork")
    assert outcome.status == "failed"
    assert context.conversation is not None
    source = await agent.conversations.get(context.conversation.id)
    assert source.external_id == "thread-new"
    assert len(source.runs) == 1
    assert len(await agent.threads.list_threads(user_id=context.user_id)) == 1
    assert len(FakeCodex.turn_calls) == 1


async def test_fork_of_an_im_run_uses_the_authorized_parent_surface(
    execution: tuple[CodexTentacle, CommandContext, AsyncMock],
) -> None:
    agent, context, _ = execution
    child = await agent.threads.enter(context.address)
    assert child.parent_thread_id is not None
    context = replace(
        context,
        conversation=await agent.conversations.ensure(
            child.id, agent_tentacle_id=agent.id
        ),
    )
    context = await recorded_context(agent, context)
    assert (await deliver(agent, context, "fork")).status == "completed"
    assert len(await agent.threads.list_threads(user_id=context.user_id)) == 2
