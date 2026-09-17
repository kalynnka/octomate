from __future__ import annotations

import asyncio
import sys
import uuid
from dataclasses import replace
from pathlib import Path
from typing import Literal

import httpx
import pytest
from pydantic_ai.exceptions import AgentRunError
from sqlalchemy.ext.asyncio import AsyncEngine

from octomate import Octomate
from octomate.auth import current_user
from octomate.config.channels import TrunklineChannelConfig
from octomate.schemas.awakes import DeferredActionBatchResponse
from octomate.schemas.conversation import ChannelAddress
from octomate.schemas.deferred import DeferredActionBatch, DeferredApproval
from octomate.schemas.events import MessageEvent
from octomate.schemas.segments import TextSegment
from octomate.schemas.user import User, UserProfile
from octomate.tentacles.trunkline import TrunklineTentacle
from octomate.tentacles.zcode import ZcodeTentacle
from octomate.tentacles.zcode.base import ZcodeBridgeContext
from octomate.tentacles.zcode.wire import (
    InteractionOrigin,
    QuestionItem,
    QuestionOption,
    QuestionSchema,
    json_object_adapter,
)
from tests.support.users import a_user
from tests.support.zcode import (
    INTERACTIVE_SERVER,
    desktop_config,
    permission_request,
    question_request,
)

ADDRESS = ChannelAddress(
    channel_tentacle_id="trunkline",
    chat_type="dm",
    chat_id="dev",
    channel_thread_id="zcode-test",
    user_id="dev",
)


class PendingWaiters(dict[uuid.UUID, asyncio.Future[DeferredActionBatchResponse]]):
    def __init__(self) -> None:
        super().__init__()
        self.changed: asyncio.Event = asyncio.Event()

    def __setitem__(
        self, key: uuid.UUID, value: asyncio.Future[DeferredActionBatchResponse]
    ) -> None:
        super().__setitem__(key, value)
        self.changed.set()


@pytest.fixture
async def tentacle(in_memory_engine: AsyncEngine, tmp_path: Path) -> ZcodeTentacle:
    octomate = Octomate()
    config = desktop_config(tmp_path)
    config.approval_timeout = 3
    config.command = [sys.executable, "-u", "-c", INTERACTIVE_SERVER]
    agent = ZcodeTentacle("zcode", octomate, config=config)
    agent.pending = PendingWaiters()
    octomate.connect(agent)
    channel = TrunklineTentacle(
        "trunkline",
        octomate,
        config=TrunklineChannelConfig(
            agents=["zcode"],
        ),
    )
    octomate.connect(channel)
    await channel.probe()
    return agent


@pytest.fixture
async def context(tentacle: ZcodeTentacle) -> ZcodeBridgeContext:
    thread = await tentacle.octomate.thread_manager.ensure(ADDRESS)
    conversation = await tentacle.octomate.conversations.ensure(
        thread.id, agent_tentacle_id="zcode"
    )
    return ZcodeBridgeContext(conversation, ADDRESS, "react", True, set(), "session-1")


async def pending_batch(tentacle: ZcodeTentacle) -> DeferredActionBatch:
    assert isinstance(tentacle.pending, PendingWaiters)
    await asyncio.wait_for(tentacle.pending.changed.wait(), 3)
    tentacle.pending.changed.clear()
    return await tentacle.octomate.deferred_actions.get_batch(
        next(iter(tentacle.pending))
    )


@pytest.mark.parametrize("approved", [True, False])
async def test_approval_uses_existing_cards_and_live_delivery(
    tentacle: ZcodeTentacle,
    context: ZcodeBridgeContext,
    approved: bool,
) -> None:
    task = asyncio.create_task(
        tentacle.answer_interaction(context, permission_request())
    )
    batch = await pending_batch(tentacle)
    action = next(iter(batch.approvals))
    assert action.args.tool_name == "Bash"
    assert action.args.args == {
        "input": {"command": "pwd"},
        "reason": "Review the command",
        "riskLevel": "low",
    }
    assert batch.target_address == ADDRESS
    assert batch.target_mode == "sub"
    await tentacle.octomate.kick(
        DeferredActionBatchResponse(
            batch_id=batch.id,
            approvals={action.id: approved},
        )
    )
    response = await asyncio.wait_for(task, 2)
    assert response["decision"] == ("allow" if approved else "deny")
    assert "permissionUpdates" not in response
    stored = await tentacle.octomate.deferred_actions.get_batch(batch.id)
    assert stored.status == "resolved"
    assert next(iter(stored.approvals)).result == approved
    assert not tentacle.pending


async def test_multiple_questions_preserve_choices_values_and_text(
    tentacle: ZcodeTentacle,
    context: ZcodeBridgeContext,
) -> None:
    request = question_request()
    request.params.questions[0].options.extend(
        [
            QuestionOption(value="other", label="Other", description="Another branch"),
            QuestionOption(value="third", label="Third"),
            QuestionOption(value="fourth", label="Fourth", preview="Full preview"),
        ]
    )
    request.params.questions.append(
        QuestionItem(
            question="Which tests?",
            header="Tests",
            multi_select=True,
            options=[
                QuestionOption(value="unit", label="Unit"),
                QuestionOption(value="integration", label="Integration"),
            ],
        )
    )
    context.session_allowed.add("AskUserQuestion")
    task = asyncio.create_task(tentacle.answer_interaction(context, request))
    batch = await pending_batch(tentacle)
    first, second = sorted(batch.questions)
    assert "choices" in first.args
    assert "hint" in first.args
    assert "hint" in second.args
    assert first.args["choices"] == ["Main", "Other", "Third"]
    assert "Fourth" in first.args["hint"]
    assert "Full preview" in first.args["hint"]
    assert "Use the default branch" in first.args["hint"]
    assert "multiple answers" in second.args["hint"]
    await tentacle.octomate.kick(
        DeferredActionBatchResponse(
            batch_id=batch.id,
            answers={first.id: "Main", second.id: "Unit, Integration, custom check"},
        )
    )
    assert await task == {
        "action": "accept",
        "content": {
            "answer_0": "main",
            "answer_1": "Unit, Integration, custom check",
        },
    }


@pytest.mark.parametrize(
    ("answer", "expected"),
    [("Approve", "approve"), ("Revise the tests first", "Revise the tests first")],
)
async def test_plan_review_includes_full_plan_and_preserves_feedback(
    tentacle: ZcodeTentacle,
    context: ZcodeBridgeContext,
    answer: str,
    expected: str,
) -> None:
    request = question_request()
    request.params.tool_name = "ExitPlanMode"
    request.params.request_schema = QuestionSchema(interaction="plan_approval")
    request.params.input = {"plan": "1. Inspect the repository.\n2. Run the tests."}
    request.params.questions = [
        QuestionItem(
            question="Review this implementation plan.",
            header="Plan",
            options=[QuestionOption(value="approve", label="Approve")],
        )
    ]
    task = asyncio.create_task(tentacle.answer_interaction(context, request))
    batch = await pending_batch(tentacle)
    action = next(iter(batch.questions))
    assert "hint" in action.args
    assert "1. Inspect the repository.\n2. Run the tests." in action.args["hint"]
    await tentacle.octomate.kick(
        DeferredActionBatchResponse(batch_id=batch.id, answers={action.id: answer})
    )
    assert await task == {"action": "accept", "content": {"answer_0": expected}}


@pytest.mark.parametrize("kind", ["approval", "question"])
async def test_timeout_expires_and_unblocks(
    tentacle: ZcodeTentacle,
    context: ZcodeBridgeContext,
    kind: str,
) -> None:
    tentacle.config.approval_timeout = 0.1
    request = permission_request() if kind == "approval" else question_request()
    task = asyncio.create_task(tentacle.answer_interaction(context, request))
    batch = await pending_batch(tentacle)
    result = await asyncio.wait_for(task, 2)
    assert "expired" in str(result["reason"])
    stored = await tentacle.octomate.deferred_actions.get_batch(batch.id)
    assert stored.status == "expired"
    assert not tentacle.pending


@pytest.mark.parametrize(
    "unavailable", ["noninteractive", "no_channel", "foreign_session"]
)
async def test_unavailable_human_and_foreign_session_decline_without_cards(
    tentacle: ZcodeTentacle,
    context: ZcodeBridgeContext,
    unavailable: str,
) -> None:
    context.session_allowed.add("Bash")
    if unavailable == "noninteractive":
        context.interactive = False
    elif unavailable == "no_channel":
        tentacle.octomate.tentacles.pop("trunkline")
    else:
        context.session_id = "another-session"
    permission = await tentacle.answer_interaction(context, permission_request())
    question = await tentacle.answer_interaction(context, question_request())
    assert permission["decision"] == "deny"
    assert question["action"] == "decline"
    assert not await tentacle.octomate.deferred_actions.pending_for_thread(
        context.conversation.thread_id
    )


async def test_subagent_origin_belongs_to_parent_session(
    tentacle: ZcodeTentacle,
    context: ZcodeBridgeContext,
) -> None:
    request = permission_request("child-session")
    request.params.origin = InteractionOrigin(
        kind="subagent", parent_session_id="session-1"
    )
    context.session_allowed.add("Bash")
    assert await tentacle.answer_interaction(context, request) == {"decision": "allow"}


async def test_empty_and_prompt_only_questions(
    tentacle: ZcodeTentacle,
    context: ZcodeBridgeContext,
) -> None:
    request = question_request()
    request.params.questions = []
    request.params.prompt = "Explain the requirement"
    task = asyncio.create_task(tentacle.answer_interaction(context, request))
    batch = await pending_batch(tentacle)
    action = next(iter(batch.questions))
    assert action.args["question"] == "Explain the requirement"
    await tentacle.octomate.kick(
        DeferredActionBatchResponse(batch_id=batch.id, answers={action.id: ""})
    )
    assert (await task)["action"] == "decline"


async def test_full_run_web_resolution_resume_grants_and_isolation(
    tentacle: ZcodeTentacle,
    context: ZcodeBridgeContext,
) -> None:
    task = asyncio.create_task(
        tentacle.run(
            "approval",
            conversation_address=ADDRESS,
            thread_id=context.conversation.thread_id,
        )
    )
    batch = await pending_batch(tentacle)
    [client] = tentacle.live_clients
    assert len(client.interactions) == 1
    assert (
        len(
            await tentacle.octomate.deferred_actions.pending_for_thread(
                context.conversation.thread_id
            )
        )
        == 1
    )
    action = next(iter(batch.approvals))
    assert "secret-test-key" not in action.model_dump_json()
    assert "[redacted]" in action.model_dump_json()
    user = await a_user("dev", profiles={"trunkline": "dev"})
    await tentacle.octomate.thread_manager.record_inbound(
        MessageEvent(
            tentacle_id=ADDRESS.channel_tentacle_id,
            chat_id=ADDRESS.chat_id,
            chat_type=ADDRESS.chat_type,
            channel_thread_id=ADDRESS.channel_thread_id,
            user_id=ADDRESS.user_id,
            sender=UserProfile(channel_user_id=ADDRESS.user_id),
            segments=[TextSegment(data={"text": "approval"})],
        )
    )

    def authenticated_user() -> User:
        return user

    tentacle.octomate.dependency_overrides[current_user] = authenticated_user
    transport = httpx.ASGITransport(app=tentacle.octomate)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://testserver",
        headers={"X-Octomate-Request": "1"},
    ) as web:
        response = await web.post(
            f"/api/trunkline/batches/{batch.id}/resolve",
            json={
                "approvals": {str(action.id): True},
                "allow_session": True,
            },
        )
        assert response.status_code == 200
        result = await asyncio.wait_for(task, 3)
        assert json_object_adapter.validate_json(result.output) == {"decision": "allow"}
        again = await web.post(
            f"/api/trunkline/batches/{batch.id}/resolve",
            json={"approvals": {str(action.id): True}},
        )
        assert again.status_code == 409
    saved = await tentacle.octomate.conversations.get(context.conversation.id)
    assert saved.allowed_tools == ["Bash"]
    session_id = saved.external_id
    result = await asyncio.wait_for(
        tentacle.run(
            "approval",
            conversation_address=ADDRESS,
            thread_id=saved.thread_id,
        ),
        3,
    )
    assert json_object_adapter.validate_json(result.output) == {"decision": "allow"}
    saved = await tentacle.octomate.conversations.get(saved.id)
    assert saved.external_id == session_id
    assert len(saved.runs) == 2
    assert [len(run.messages) for run in saved.runs] == [2, 2]
    assert not tentacle.pending
    other_address = replace(ADDRESS, channel_thread_id="another-thread")
    other_thread = await tentacle.octomate.thread_manager.ensure(other_address)
    other = await tentacle.octomate.conversations.ensure(
        other_thread.id, agent_tentacle_id="zcode"
    )
    assert other.allowed_tools == []
    isolated = replace(
        context, conversation=other, address=other_address, session_allowed=set()
    )
    task = asyncio.create_task(
        tentacle.answer_interaction(isolated, permission_request())
    )
    batch = await pending_batch(tentacle)
    assert batch.conversation_id == other.id
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.mark.parametrize("end", ["cancel", "shutdown", "exit"])
async def test_waiting_run_cleanup_expires_card_and_preserves_history(
    tentacle: ZcodeTentacle,
    context: ZcodeBridgeContext,
    end: Literal["cancel", "shutdown", "exit"],
) -> None:
    task = asyncio.create_task(
        tentacle.run(
            "ask",
            conversation_address=ADDRESS,
            thread_id=context.conversation.thread_id,
        )
    )
    batch = await pending_batch(tentacle)
    [client] = tentacle.live_clients
    assert client.process is not None
    if end == "cancel":
        task.cancel()
    elif end == "shutdown":
        await tentacle.__aexit__(None, None, None)
    else:
        client.process.kill()
    with pytest.raises((asyncio.CancelledError, AgentRunError)):
        await asyncio.wait_for(task, 3)
    stored = await tentacle.octomate.deferred_actions.get_batch(batch.id)
    assert stored.status == "expired"
    assert not tentacle.pending
    assert not client.callback_tasks
    assert not client.interactions
    assert client.process.returncode is not None
    saved = await tentacle.octomate.conversations.get(context.conversation.id)
    assert len(saved.runs) == 1


async def test_full_run_answers_question_without_starting_another_run(
    tentacle: ZcodeTentacle,
    context: ZcodeBridgeContext,
) -> None:
    task = asyncio.create_task(
        tentacle.run(
            "ask",
            conversation_address=ADDRESS,
            thread_id=context.conversation.thread_id,
        )
    )
    batch = await pending_batch(tentacle)
    action = next(iter(batch.questions))
    await tentacle.octomate.kick(
        DeferredActionBatchResponse(batch_id=batch.id, answers={action.id: "Main"})
    )
    result = await asyncio.wait_for(task, 3)
    assert json_object_adapter.validate_json(result.output) == {
        "action": "accept",
        "content": {"answer_0": "main"},
    }
    saved = await tentacle.octomate.conversations.get(context.conversation.id)
    assert len(saved.runs) == 1


async def test_cancellation_while_presenting_expires_persisted_batch(
    tentacle: ZcodeTentacle,
    context: ZcodeBridgeContext,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    presenting, release = asyncio.Event(), asyncio.Event()

    async def present(
        address: ChannelAddress, actions: list[DeferredApproval]
    ) -> dict[uuid.UUID, str | None]:
        presenting.set()
        await release.wait()
        return {action.id: None for action in actions}

    channel = tentacle.octomate.channels["trunkline"]
    monkeypatch.setattr(channel.feelers.approvals, "present", present)
    task = asyncio.create_task(
        tentacle.answer_interaction(context, permission_request())
    )
    await asyncio.wait_for(presenting.wait(), 2)
    [batch] = await tentacle.octomate.deferred_actions.pending_for_thread(
        context.conversation.thread_id
    )
    task.cancel()
    await asyncio.sleep(0)
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    stored = await tentacle.octomate.deferred_actions.get_batch(batch.id)
    assert stored.status == "expired"
    assert not tentacle.pending
