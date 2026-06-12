"""Unit tests for the human-in-the-loop deferred suspender.

`HumanReviewSuspender` is the policy react invokes (via `ResolveDeferred`) when an
agent run yields `DeferredToolRequests` and no in-process resolver is configured:
it persists a batch + presents it through the channel, then records the batch id
so the caller can report the suspended run.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import cast

from pydantic_ai.messages import ToolCallPart
from pydantic_ai.tools import DeferredToolRequests

from octomate.capabilities.events import ActionBatchEvent
from octomate.managers.conversations import ConversationManager
from octomate.managers.deferred import DeferredActionManager
from octomate.schemas.conversation import ConversationKey
from octomate.schemas.deferred import (
    ApprovalRequest,
    DeferredApproval,
    DeferredQuestion,
)
from octomate.schemas.triage import ResponseTargetMode, TriageDecision
from octomate.tentacles.agent.graph.suspender import HumanReviewSuspender
from octomate.tentacles.channel.base import ChannelTentacle
from octomate.tentacles.channel.feelers.base import Feelers
from octomate.tentacles.channel.feelers.deferred import (
    PlainTextApprovalFeeler,
    PlainTextAskQuestionFeeler,
)
from octomate.tentacles.channel.feelers.output import TimelineState


@dataclass
class FakeConversation:
    id: uuid.UUID = field(default_factory=uuid.uuid4)


@dataclass
class FakeConversationManager:
    ensured: list[tuple[ConversationKey, str | None]] = field(default_factory=list)

    async def ensure(
        self,
        key: ConversationKey,
        *,
        agent_tentacle_id: str | None = None,
    ) -> FakeConversation:
        self.ensured.append((key, agent_tentacle_id))
        return FakeConversation()


@dataclass
class CreateBatchCall:
    run_name: str | None
    source_key: ConversationKey
    target_key: ConversationKey
    target_mode: ResponseTargetMode
    decision: TriageDecision | None
    requests: DeferredToolRequests
    batch_id: uuid.UUID


@dataclass
class FakePresentedBatch:
    id: uuid.UUID = field(default_factory=uuid.uuid4)
    questions: list[object] = field(default_factory=list)
    approvals: list[object] = field(default_factory=list)


@dataclass
class FakeActionManager:
    create_calls: list[CreateBatchCall] = field(default_factory=list)

    async def create_batch(
        self,
        *,
        conversation: FakeConversation,
        agent_tentacle_id: str,
        run_name: str | None,
        source_key: ConversationKey,
        target_key: ConversationKey,
        target_mode: ResponseTargetMode,
        decision: TriageDecision | None,
        requests: DeferredToolRequests,
    ) -> FakePresentedBatch:
        batch = FakePresentedBatch()
        self.create_calls.append(
            CreateBatchCall(
                run_name=run_name,
                source_key=source_key,
                target_key=target_key,
                target_mode=target_mode,
                decision=decision,
                requests=requests,
                batch_id=batch.id,
            )
        )
        return batch

    async def mark_action_presented(
        self,
        action_id: uuid.UUID,
        platform_message_id: str | None,
    ) -> None:
        return None


class NoopTimeline(TimelineState):
    @asynccontextmanager
    async def open(self, key: ConversationKey) -> AsyncIterator[NoopTimeline]:
        yield self


@dataclass
class FakeChannel:
    sent: list[tuple[ConversationKey, str]] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.feelers = Feelers[str](
            markdown=self,
            markdown_stream=self,
            timeline=NoopTimeline(),
            approvals=PlainTextApprovalFeeler(self),
            ask_questions=PlainTextAskQuestionFeeler(self),
        )

    async def present(self, key: ConversationKey, markdown: str) -> str | None:
        self.sent.append((key, markdown))
        return None


def _key() -> ConversationKey:
    return ConversationKey(
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
    key = _key()
    requests = _requests()
    conversations = FakeConversationManager()
    action_manager = FakeActionManager()
    channel = FakeChannel()
    decision = TriageDecision(action="reception", target_id="im")

    suspender = HumanReviewSuspender(
        channel=cast(ChannelTentacle, channel),
        action_manager=cast(DeferredActionManager, action_manager),
        conversation_manager=cast(ConversationManager, conversations),
        agent_tentacle_id="inkling",
        run_name="reception",
        source_key=key,
        target_key=key,
        target_mode="sub",
        decision=decision,
    )

    assert suspender.suspended_batch_id is None
    await suspender.suspend(requests)

    assert len(action_manager.create_calls) == 1
    call = action_manager.create_calls[0]
    assert call.run_name == "reception"
    assert call.source_key == key
    assert call.target_key == key
    assert call.target_mode == "sub"
    assert call.decision is decision
    assert call.requests is requests
    assert suspender.suspended_batch_id == call.batch_id
    assert conversations.ensured == [(key, "inkling")]


@dataclass
class FakeBatch:
    id: uuid.UUID = field(default_factory=uuid.uuid4)
    questions: list[DeferredQuestion] = field(default_factory=list)
    approvals: list[DeferredApproval] = field(default_factory=list)


@dataclass
class EmitActionManager:
    """Persists (returns a preset batch) but never renders — the emit path."""

    batch: FakeBatch

    async def create_batch(
        self,
        *,
        conversation: FakeConversation,
        agent_tentacle_id: str,
        run_name: str | None,
        source_key: ConversationKey,
        target_key: ConversationKey,
        target_mode: ResponseTargetMode,
        decision: TriageDecision | None,
        requests: DeferredToolRequests,
    ) -> FakeBatch:
        return self.batch


async def test_suspender_emit_on_stream_returns_batch_event_without_rendering() -> None:
    key = _key()
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
    batch = FakeBatch(questions=[question], approvals=[approval])
    channel = FakeChannel()

    suspender = HumanReviewSuspender(
        channel=cast(ChannelTentacle, channel),
        action_manager=cast(DeferredActionManager, EmitActionManager(batch)),
        conversation_manager=cast(ConversationManager, FakeConversationManager()),
        agent_tentacle_id="inkling",
        run_name="reception",
        source_key=key,
        target_key=key,
        target_mode="sub",
        decision=None,
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
