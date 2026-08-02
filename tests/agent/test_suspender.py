"""Unit tests for the human-in-the-loop deferred suspender.

`HumanReviewSuspender` is the policy react invokes (via `ResolveDeferred`) when an
agent run yields `DeferredToolRequests` and no in-process resolver is configured:
it persists a batch + presents it through the channel, then records the batch id
so the caller can report the suspended run.
"""

from __future__ import annotations

from typing import cast

from pydantic_ai.messages import ToolCallPart
from pydantic_ai.tools import DeferredToolRequests
from uuid_utils.compat import uuid7

from octomate.capabilities.harness.events import ActionBatchEvent
from octomate.managers.deferred import DeferredActionManager
from octomate.reflex.suspender import HumanReviewSuspender
from octomate.schemas.conversation import ChannelAddress
from octomate.schemas.deferred import (
    ApprovalRequest,
    DeferredApproval,
    DeferredQuestion,
)
from octomate.schemas.triage import SummonDecision
from tests.support.channels import FakeChannelTentacle
from tests.support.managers import (
    FakeActionManager,
    FakeConversationManager,
    FakePresentedBatch,
)


def _key() -> ChannelAddress:
    return ChannelAddress(
        channel_tentacle_id="im",
        chat_type="private",
        chat_id="alice",
        user_id="alice",
    )


def _requests() -> DeferredToolRequests:
    return DeferredToolRequests(
        calls=[
            ToolCallPart(
                tool_name="ask_questions",
                args={"questions": [{"question": "What should I clarify?"}]},
                tool_call_id="call_question",
            )
        ]
    )


async def test_human_review_suspender_persists_batch_and_records_id() -> None:
    address = _key()
    thread_id = uuid7()
    requests = _requests()
    conversations = FakeConversationManager()
    action_manager = FakeActionManager()
    channel = FakeChannelTentacle()
    decision = SummonDecision(
        action="summon",
        agent_id="inkling",
        model="test",
        reason="needs input",
        hint="needs input",
        summon="needs input",
    )

    suspender = HumanReviewSuspender(
        channel=channel,
        action_manager=cast(DeferredActionManager, action_manager),
        conversation_manager=conversations,
        agent_tentacle_id="inkling",
        run_name="react",
        source_address=address,
        target_address=address,
        target_mode="sub",
        decision=decision,
        thread_id=thread_id,
    )

    assert suspender.suspended_batch_id is None
    await suspender.suspend(requests)

    assert len(action_manager.create_calls) == 1
    call = action_manager.create_calls[0]
    assert call.run_name == "react"
    assert call.source_address == address
    assert call.target_address == address
    assert call.target_mode == "sub"
    assert call.decision is decision
    assert call.requests is requests
    assert suspender.suspended_batch_id == call.batch_id
    assert conversations.ensured == [(thread_id, "inkling")]


async def test_suspender_emit_on_stream_returns_batch_event_without_rendering() -> None:
    address = _key()
    question = DeferredQuestion(
        tool_name="ask_questions",
        tool_call_id="c1",
        args={"question": "What should I clarify?"},
    )
    approval = DeferredApproval(
        tool_name="do_thing",
        tool_call_id="c2",
        args=ApprovalRequest(tool_name="do_thing"),
    )
    batch = FakePresentedBatch(questions=[question], approvals=[approval])
    channel = FakeChannelTentacle()

    suspender = HumanReviewSuspender(
        channel=channel,
        action_manager=cast(
            DeferredActionManager, FakeActionManager(presented_batch=batch)
        ),
        conversation_manager=FakeConversationManager(),
        agent_tentacle_id="inkling",
        run_name="react",
        source_address=address,
        target_address=address,
        target_mode="sub",
        decision=None,
        thread_id=uuid7(),
        emit_on_stream=True,
    )

    event = await suspender.suspend(_requests())

    # Persisted (batch id recorded) and handed back as one event — rendered nothing
    # through the channel (that is the consumer's job).
    assert suspender.suspended_batch_id == batch.id
    assert channel.sent == []

    assert isinstance(event, ActionBatchEvent)
    assert event.batch_id == str(batch.id)
    assert event.questions == [question]
    assert event.approvals == [approval]
