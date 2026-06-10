"""Unit tests for the human-in-the-loop deferred suspender.

`HumanReviewSuspender` is the policy react invokes (via `ResolveDeferred`) when an
agent run yields `DeferredToolRequests` and no in-process resolver is configured:
it persists a batch + presents it through the channel, then records the batch id
so the caller can report the suspended run.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import cast

from pydantic_ai.tools import DeferredToolRequests
from pydantic_ai.messages import ToolCallPart

from octomate.managers.conversations import ConversationManager
from octomate.managers.deferred import DeferredActionManager
from octomate.schemas.conversation import ConversationKey
from octomate.schemas.triage import ResponseTargetMode, TriageDecision
from octomate.tentacles.agent.graph.suspender import HumanReviewSuspender
from octomate.tentacles.channel.base import ChannelTentacle
from octomate.tentacles.channel.feelers.base import Feelers
from octomate.tentacles.channel.feelers.deferred import (
    PlainTextApprovalFeeler,
    PlainTextAskQuestionFeeler,
)


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


class NoopTimeline:
    async def open(self, key: ConversationKey) -> NoopTimeline:
        return self

    async def thinking_started(self, text: str) -> None: ...
    async def thinking_delta(self, text: str) -> None: ...
    async def tool_started(self, event: object) -> None: ...
    async def tool_finished(self, event: object) -> None: ...
    async def answer_delta(self, text: str) -> None: ...
    async def todo(self, event: object) -> None: ...
    async def finalize(self) -> str | None:
        return None


@dataclass
class FakeChannel:
    sent: list[tuple[ConversationKey, str]] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.feelers = Feelers[str](
            markdown=self,
            markdown_stream=self,
            event_stream=self,
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
