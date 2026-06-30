"""Canonical in-memory manager fakes.

`FakeConversationManager` keeps per-address message lists so graph tests run
without a database; `FakeActionManager` records batch lifecycle calls and
returns scriptable batches. Tests of the *real* managers use the actual
classes with the `in_memory_engine` fixture instead.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import cast

from pydantic_ai.messages import ModelMessage
from pydantic_ai.tools import DeferredToolRequests, DeferredToolResults
from uuid_utils.compat import uuid7

from octomate.config.agents import AgentRouteModelName
from octomate.managers.conversation import ConversationManager
from octomate.managers.thread import ThreadManager, message_text_from_segments
from octomate.schemas.awakes import DeferredActionBatchResponse
from octomate.schemas.channel import (
    ChannelActorKind,
    Handoff,
    MessageBinding,
    Thread,
    ThreadKey,
    ThreadMessage,
)
from octomate.schemas.conversation import (
    ChannelAddress,
    Conversation,
    ConversationPermissionMode,
    UserProfile,
)
from octomate.schemas.deferred import DeferredApproval, DeferredQuestion
from octomate.schemas.runs import AgentRun
from octomate.schemas.segments import MessageSegment
from octomate.schemas.triage import ResponseTargetMode, TriageDecision
from octomate.types.deferred import DeferredBatchStatus


@dataclass
class FakeConversation:
    """Stand-in whose `messages` is a plain list react can read + accumulate
    into, since the real arcanus relation can't be appended to detached."""

    id: uuid.UUID = field(default_factory=uuid.uuid4)
    messages: list[ModelMessage] = field(default_factory=list)
    external_id: str | None = None
    thread_id: uuid.UUID | None = None
    permission_mode: ConversationPermissionMode = "default"
    allowed_tools: list[str] = field(default_factory=list)


@dataclass
class FakeConversationManager(ConversationManager):
    store: dict[tuple[ChannelAddress, str | None], FakeConversation] = field(
        default_factory=dict
    )
    ensured: list[tuple[ChannelAddress, str | None]] = field(default_factory=list)
    runs: list[tuple[FakeConversation, str, list[ModelMessage]]] = field(
        default_factory=list
    )

    async def ensure(
        self,
        address: ChannelAddress,
        *,
        agent_tentacle_id: str | None = None,
        thread_id: uuid.UUID | None = None,
    ) -> Conversation:
        self.ensured.append((address, agent_tentacle_id))
        store_key = (address, agent_tentacle_id)
        conversation = self.store.get(store_key)
        if conversation is None:
            conversation = FakeConversation(thread_id=thread_id)
            self.store[store_key] = conversation
        elif thread_id is not None:
            if conversation.thread_id is None:
                conversation.thread_id = thread_id
            elif conversation.thread_id != thread_id:
                raise ValueError(
                    "conversation is already attached to a different thread"
                )
        return cast(Conversation, conversation)

    async def thread_id(self, conversation_id: uuid.UUID) -> uuid.UUID | None:
        for conversation in self.store.values():
            if conversation.id == conversation_id:
                return conversation.thread_id
        return None

    async def record_agent_run(
        self,
        conversation: Conversation,
        run_id: str,
        messages: Sequence[ModelMessage],
        *,
        name: str | None = None,
        external_id: str | None = None,
    ) -> AgentRun | None:
        fake = cast(FakeConversation, conversation)
        self.runs.append((fake, f"{name}:{run_id}", list(messages)))
        fake.messages.extend(messages)
        if external_id is not None:
            fake.external_id = external_id
        return None

    async def grant_session_tool(
        self,
        conversation: Conversation,
        tool_name: str,
    ) -> None:
        fake = cast(FakeConversation, conversation)
        if tool_name not in fake.allowed_tools:
            fake.allowed_tools.append(tool_name)

    async def drop_trailing_deferral(
        self,
        conversation: Conversation,
    ) -> None:
        return None


@dataclass
class FakeThreadManager(ThreadManager):
    threads_by_key: dict[ThreadKey, Thread] = field(default_factory=dict)
    handoffs: list[Handoff] = field(default_factory=list)
    outbounds: list[ThreadMessage] = field(default_factory=list)
    assistant_reply_bindings: list[tuple[list[uuid.UUID], str]] = field(
        default_factory=list
    )

    async def ensure_thread(
        self,
        address_or_key: ChannelAddress | ThreadKey,
    ) -> Thread:
        key = (
            ThreadKey.from_address(address_or_key)
            if isinstance(address_or_key, ChannelAddress)
            else address_or_key
        )
        thread = self.threads_by_key.get(key)
        if thread is None:
            thread = Thread(
                id=uuid7(),
                channel_tentacle_id=key.channel_tentacle_id,
                chat_type=key.chat_type,
                chat_id=key.chat_id,
                thread_id=key.thread_id,
            )
            self.threads_by_key[key] = thread
        return thread

    async def record_handoff(
        self,
        thread_or_address: Thread | ChannelAddress | ThreadKey,
        *,
        to_agent_tentacle_id: str,
        from_agent_tentacle_id: str | None = None,
        to_model: AgentRouteModelName | None = None,
        reason: str = "",
        hint: str = "",
        brief: str = "",
        source_conversation_id: uuid.UUID | None = None,
        target_conversation_id: uuid.UUID | None = None,
        source_run_id: str | None = None,
        source_model_message_id: uuid.UUID | None = None,
    ) -> Handoff:
        if isinstance(thread_or_address, Thread):
            thread = thread_or_address
        else:
            thread = await self.ensure_thread(thread_or_address)
        handoff = Handoff(
            id=uuid7(),
            thread_id=thread.id,
            from_agent_tentacle_id=from_agent_tentacle_id,
            to_agent_tentacle_id=to_agent_tentacle_id,
            to_model=to_model,
            reason=reason,
            hint=hint,
            brief=brief,
            source_conversation_id=source_conversation_id,
            target_conversation_id=target_conversation_id,
            source_run_id=source_run_id,
            source_model_message_id=source_model_message_id,
        )
        thread.handoffs.append(handoff)
        self.handoffs.append(handoff)
        return handoff

    async def record_outbound(
        self,
        thread_or_address: Thread | ChannelAddress | ThreadKey,
        *,
        agent_tentacle_id: str,
        segments: list[MessageSegment],
        platform_message_id: str | None = None,
        reply_id: str = "",
        timestamp: datetime | None = None,
        sender: UserProfile | None = None,
        actor_kind: ChannelActorKind = "agent",
        message_text: str | None = None,
        raw: str = "",
    ) -> ThreadMessage:
        if isinstance(thread_or_address, Thread):
            thread = thread_or_address
        else:
            thread = await self.ensure_thread(thread_or_address)
        message = ThreadMessage(
            id=uuid7(),
            thread_id=thread.id,
            platform_message_id=platform_message_id,
            reply_id=reply_id,
            timestamp=timestamp,
            direction="outbound",
            actor_kind=actor_kind,
            user_id="",
            agent_tentacle_id=agent_tentacle_id,
            sender=sender
            or UserProfile(user_id=agent_tentacle_id, name=agent_tentacle_id),
            segments=segments,
            message_text=message_text or message_text_from_segments(segments),
            raw=raw,
        )
        thread.messages.append(message)
        self.outbounds.append(message)
        return message

    async def mark_presented(
        self,
        message: ThreadMessage,
        platform_message_id: str | None,
    ) -> ThreadMessage:
        message.platform_message_id = platform_message_id
        return message

    async def bind_assistant_replies(
        self,
        thread_message_ids: list[uuid.UUID],
        *,
        run_id: str,
    ) -> list[MessageBinding]:
        self.assistant_reply_bindings.append((list(thread_message_ids), run_id))
        return []


@dataclass
class CreateBatchCall:
    conversation: Conversation
    agent_tentacle_id: str
    run_name: str | None
    source_address: ChannelAddress
    target_address: ChannelAddress
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

    source_address: ChannelAddress
    target_address: ChannelAddress
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
        source_address: ChannelAddress,
        target_address: ChannelAddress,
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
                source_address=source_address,
                target_address=target_address,
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
