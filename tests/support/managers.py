"""Canonical in-memory manager fakes.

`FakeConversationManager` keeps per-key message lists so graph tests run
without a database; `FakeActionManager` records batch lifecycle calls and
returns scriptable batches. Tests of the *real* managers use the actual
classes with the `in_memory_engine` fixture instead.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import cast

from pydantic_ai.messages import ModelMessage
from pydantic_ai.tools import DeferredToolRequests, DeferredToolResults

from octomate.managers.conversations import ConversationManager
from octomate.schemas.awakes import DeferredActionBatchResponse
from octomate.schemas.conversation import Conversation, ConversationKey
from octomate.schemas.deferred import DeferredApproval, DeferredQuestion
from octomate.schemas.triage import ResponseTargetMode, TriageDecision
from octomate.types.deferred import DeferredBatchStatus


@dataclass
class FakeConversation:
    """Stand-in whose `messages` is a plain list react can read + accumulate
    into, since the real arcanus relation can't be appended to detached."""

    id: uuid.UUID = field(default_factory=uuid.uuid4)
    messages: list[ModelMessage] = field(default_factory=list)


@dataclass
class FakeConversationManager(ConversationManager):
    store: dict[ConversationKey, FakeConversation] = field(default_factory=dict)
    ensured: list[tuple[ConversationKey, str | None]] = field(default_factory=list)
    runs: list[tuple[FakeConversation, str, list[ModelMessage]]] = field(
        default_factory=list
    )

    async def ensure(
        self,
        key: ConversationKey,
        *,
        agent_tentacle_id: str | None = None,
    ) -> Conversation:
        self.ensured.append((key, agent_tentacle_id))
        conversation = self.store.get(key)
        if conversation is None:
            conversation = FakeConversation()
            self.store[key] = conversation
        return cast(Conversation, conversation)

    async def record_agent_run(
        self,
        conversation: Conversation,
        run_id: str,
        messages: Sequence[ModelMessage],
        *,
        name: str | None = None,
    ) -> None:
        fake = cast(FakeConversation, conversation)
        self.runs.append((fake, f"{name}:{run_id}", list(messages)))
        fake.messages.extend(messages)

    async def drop_trailing_deferral(
        self,
        conversation: Conversation,
    ) -> None:
        return None


@dataclass
class CreateBatchCall:
    conversation: Conversation
    agent_tentacle_id: str
    run_name: str | None
    source_key: ConversationKey
    target_key: ConversationKey
    target_mode: ResponseTargetMode
    decision: TriageDecision | None
    requests: DeferredToolRequests
    batch_id: uuid.UUID


@dataclass
class FakePresentedBatch:
    """What `create_batch` hands back: the persisted actions to present."""

    id: uuid.UUID = field(default_factory=uuid.uuid4)
    questions: list[DeferredQuestion] = field(default_factory=list)
    approvals: list[DeferredApproval] = field(default_factory=list)


@dataclass
class FakeDeferredBatch:
    """What `resolve_batch`/`get_batch` hand back: a resumable batch."""

    source_key: ConversationKey
    target_key: ConversationKey
    requests: DeferredToolRequests
    deferred_results: DeferredToolResults
    id: uuid.UUID = field(default_factory=uuid.uuid4)
    agent_tentacle_id: str = "inkling"
    run_name: str | None = "triage"
    target_mode: ResponseTargetMode = "main"
    decision: TriageDecision | None = None
    status: DeferredBatchStatus = "resolved"
    completed: bool = True

    def build_results(self) -> DeferredToolResults:
        return self.deferred_results


@dataclass
class FakeActionManager:
    batch: FakeDeferredBatch | None = None
    presented_batch: FakePresentedBatch | None = None
    create_calls: list[CreateBatchCall] = field(default_factory=list)
    presented: list[tuple[uuid.UUID, str | None]] = field(default_factory=list)
    marked: list[tuple[uuid.UUID, DeferredBatchStatus, bool]] = field(
        default_factory=list
    )

    async def create_batch(
        self,
        *,
        conversation: Conversation,
        agent_tentacle_id: str,
        run_name: str | None,
        source_key: ConversationKey,
        target_key: ConversationKey,
        target_mode: ResponseTargetMode,
        decision: TriageDecision | None,
        requests: DeferredToolRequests,
    ) -> FakePresentedBatch:
        batch = self.presented_batch or FakePresentedBatch()
        self.create_calls.append(
            CreateBatchCall(
                conversation=conversation,
                agent_tentacle_id=agent_tentacle_id,
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
        self.presented.append((action_id, platform_message_id))

    async def resolve_batch(
        self,
        awake: DeferredActionBatchResponse,
    ) -> FakeDeferredBatch:
        if self.batch is None:
            raise ValueError(f"unknown deferred action batch {awake.batch_id}")
        return self.batch

    async def get_batch(self, batch_id: uuid.UUID) -> FakeDeferredBatch:
        if self.batch is None:
            raise ValueError(f"unknown deferred action batch {batch_id}")
        return self.batch

    async def mark_batch(
        self,
        batch_id: uuid.UUID,
        status: DeferredBatchStatus,
        *,
        completed: bool = False,
    ) -> None:
        self.marked.append((batch_id, status, completed))
