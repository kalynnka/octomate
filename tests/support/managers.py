"""Canonical in-memory manager fakes, plus the real rows a test needs on disk.

`FakeConversationManager` keeps per-address message lists so graph tests run
without a database; `FakeActionManager` records batch lifecycle calls and
returns scriptable batches. Tests of the *real* managers use the actual
classes with the `in_memory_engine` fixture instead — and `a_thread` gives
them the parent row SQLite's foreign keys require.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import cast

from arcanus import Relation
from pydantic_ai.messages import ModelMessage
from pydantic_ai.tools import DeferredToolRequests, DeferredToolResults
from uuid_utils.compat import uuid7

from octomate.config.agents import AgentRouteModelName
from octomate.database import async_session
from octomate.managers.conversation import ConversationManager
from octomate.managers.project import ProjectManager
from octomate.managers.thread import ThreadManager, message_text_from_segments
from octomate.managers.user import UserManager
from octomate.schemas.awakes import DeferredActionBatchResponse
from octomate.schemas.conversation import (
    ChannelAddress,
    Conversation,
)
from octomate.schemas.deferred import DeferredApproval, DeferredQuestion
from octomate.schemas.project import Project
from octomate.schemas.runs import AgentRun
from octomate.schemas.segments import MessageSegment
from octomate.schemas.thread import (
    ChannelActorKind,
    Handoff,
    MessageBinding,
    Thread,
    ThreadKey,
    ThreadMessage,
)
from octomate.schemas.triage import ResponseTargetMode, SummonDecision
from octomate.schemas.user import UserProfile
from octomate.types.conversations import ConversationPermissionMode
from octomate.types.deferred import DeferredBatchStatus


async def a_registry(*projects: Project) -> ProjectManager:
    """A loaded registry holding `projects` — the rows a native session or a direct
    registration would have left. Nothing declares a project any more, so a test that
    wants one persists it and loads the registry over it."""
    if projects:
        async with async_session() as session:
            for project in projects:
                session.add(project)
            await session.commit()
    manager = ProjectManager()
    await manager.load()
    return manager


async def a_thread(chat_id: str = "chat") -> uuid.UUID:
    """A persisted `threads` row to hang conversations off, idempotent per
    `chat_id`. A conversation's `thread_id` is a real foreign key, so a bare
    `uuid7()` names a parent that does not exist."""
    thread = await ThreadManager(users=UserManager()).ensure(
        ThreadKey(channel_tentacle_id="test", chat_type="dm", chat_id=chat_id)
    )
    return thread.id


@dataclass
class FakeConversation:
    """Stand-in whose `messages` is a plain list react can read + accumulate
    into, since the real arcanus relation can't be appended to detached."""

    id: uuid.UUID = field(default_factory=uuid.uuid4)
    messages: list[ModelMessage] = field(default_factory=list)
    external_id: str | None = None
    thread_id: uuid.UUID | None = None
    subagent_id: str = ""
    parent_conversation_id: uuid.UUID | None = None
    agent_tentacle_id: str = ""
    runs: list[str] = field(default_factory=list)
    permission_mode: ConversationPermissionMode = "default"
    allowed_tools: list[str] = field(default_factory=list)


@dataclass
class FakeConversationManager(ConversationManager):
    store: dict[tuple[uuid.UUID, str | None, str], FakeConversation] = field(
        default_factory=dict
    )
    ensured: list[tuple[uuid.UUID, str | None]] = field(default_factory=list)
    runs: list[tuple[FakeConversation, str, list[ModelMessage]]] = field(
        default_factory=list
    )
    parent_links: list[tuple[str, str | None, str | None]] = field(default_factory=list)

    async def ensure(
        self,
        thread_id: uuid.UUID,
        *,
        agent_tentacle_id: str | None = None,
        subagent_id: str = "",
        parent_conversation_id: uuid.UUID | None = None,
    ) -> Conversation:
        self.ensured.append((thread_id, agent_tentacle_id))
        store_key = (thread_id, agent_tentacle_id, subagent_id)
        conversation = self.store.get(store_key)
        if conversation is None:
            conversation = FakeConversation(
                thread_id=thread_id,
                subagent_id=subagent_id,
                parent_conversation_id=parent_conversation_id,
                agent_tentacle_id=agent_tentacle_id or "",
            )
            self.store[store_key] = conversation
        return cast(Conversation, conversation)

    async def get(self, conversation_id: uuid.UUID) -> Conversation:
        for conversation in self.store.values():
            if conversation.id == conversation_id:
                return cast(Conversation, conversation)
        raise ValueError(f"unknown conversation {conversation_id}")

    async def link_parent_run(
        self,
        run_id: str,
        *,
        parent_run_id: str,
        parent_tool_call_id: str | None,
    ) -> None:
        self.parent_links.append((run_id, parent_run_id, parent_tool_call_id))

    async def subagents(self, parent_conversation_id: uuid.UUID) -> list[Conversation]:
        return [
            cast(Conversation, conversation)
            for conversation in self.store.values()
            if conversation.parent_conversation_id == parent_conversation_id
            and conversation.subagent_id
        ]

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
        cwd: str = "",
        external_id: str | None = None,
        parent_run_id: str | None = None,
        parent_tool_call_id: str | None = None,
    ) -> AgentRun | None:
        fake = cast(FakeConversation, conversation)
        self.runs.append((fake, f"{name}:{run_id}", list(messages)))
        fake.runs.append(run_id)
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


class FakeUserManager(UserManager):
    """In-memory profile store, keyed like the real lookup, so graph tests
    resolve channel identities without a database."""

    def __init__(self) -> None:
        super().__init__()
        self.profiles: dict[tuple[str, str], UserProfile] = {}

    async def profile(
        self, channel_tentacle_id: str, channel_user_id: str
    ) -> UserProfile | None:
        return self.profiles.get((channel_tentacle_id, channel_user_id))


@dataclass
class FakeThreadManager(ThreadManager):
    users: UserManager = field(default_factory=FakeUserManager)
    threads_by_key: dict[ThreadKey, Thread] = field(default_factory=dict)
    handoffs: list[Handoff] = field(default_factory=list)
    outbounds: list[ThreadMessage] = field(default_factory=list)
    assistant_reply_bindings: list[tuple[list[uuid.UUID], str]] = field(
        default_factory=list
    )

    async def ensure(
        self,
        address_or_key: ChannelAddress | ThreadKey,
        *,
        project: Project | None = None,
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
                channel_thread_id=key.channel_thread_id,
                kind=key.kind,
                # As the real manager does: a thread being created takes the
                # project, one that already exists keeps what it started with.
                project_id=project.id if project is not None else None,
                project=Relation(project),
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
            thread = await self.ensure(thread_or_address)
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
        happened_at: datetime | None = None,
        sender: UserProfile,
        actor_kind: ChannelActorKind = "agent",
        message_text: str | None = None,
        raw: str = "",
    ) -> ThreadMessage:
        if isinstance(thread_or_address, Thread):
            thread = thread_or_address
        else:
            thread = await self.ensure(thread_or_address)
        message = ThreadMessage(
            id=uuid7(),
            thread_id=thread.id,
            platform_message_id=platform_message_id,
            reply_id=reply_id,
            # As the real manager does: a row is never undated, because the ledger
            # orders on this.
            happened_at=happened_at or datetime.now(UTC),
            direction="outbound",
            actor_kind=actor_kind,
            user_id="",
            agent_tentacle_id=agent_tentacle_id,
            # In-memory fake: a synthetic row id plus the value pre-loaded into
            # the relation, so readers see the profile without a database.
            sender_id=uuid7(),
            sender=Relation(sender),
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
    decision: SummonDecision | None
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
    run_name: str | None = "react"
    target_mode: ResponseTargetMode = "main"
    decision: SummonDecision | None = None
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
        decision: SummonDecision | None,
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
