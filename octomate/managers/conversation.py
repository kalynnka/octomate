"""Agent conversations and their model message history."""

from __future__ import annotations

from collections import deque
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import ClassVar, Literal, TypeVar, cast

from arcanus import Cursor, Page, RelationCollection
from arcanus.materia.sqlalchemy import lazyload, noload, selectinload, undefer
from fastapi import UploadFile
from pydantic import UUID7, AwareDatetime, NonNegativeInt, TypeAdapter, validate_call
from pydantic_ai import Conversation as PydanticConversation

# Core's store uses these marks to distinguish appended messages from edited history.
from pydantic_ai._conversation_marks import mark_stored, stored_boundary
from pydantic_ai.conversation_store import (
    ConversationConflict,
    ConversationEntry,
    ConversationRecord,
)
from pydantic_ai.conversation_store import (
    ConversationStore as PydanticConversationStore,
)
from pydantic_ai.messages import ModelMessage as PydanticModelMessage
from pydantic_ai.messages import ModelMessagesTypeAdapter, ToolReturnPart
from pydantic_ai.usage import RequestUsage
from sqlalchemy import (
    Select,
    and_,
    func,
    insert,
    literal,
    or_,
    select,
)
from sqlalchemy.orm.exc import StaleDataError
from uuid_utils.compat import uuid7

from octomate.database import async_session
from octomate.managers.base import Locks, Manager
from octomate.managers.files import FileManager
from octomate.schemas.compactions import (
    Compaction,
    CompactionMembership,
    ExternalCompaction,
)
from octomate.schemas.conversation import (
    Conversation,
    ConversationCompaction,
    ConversationKey,
    ConversationRun,
)
from octomate.schemas.files import FileVariant, Jsonl
from octomate.schemas.messages import ModelMessage, ModelRequest, ModelResponse
from octomate.schemas.runs import AgentRun, ExternalAgentRun
from octomate.schemas.thread import (
    MessageBinding,
    Thread,
    ThreadLedger,
    ThreadMessage,
)
from octomate.types.compactions import (
    CompactionSessionKey,
    CompactionStage,
    CompactionTrigger,
)
from octomate.types.permissions import AgentPermissionMode

RunT = TypeVar("RunT", bound=AgentRun)

request_usage_adapter: TypeAdapter[RequestUsage] = TypeAdapter(RequestUsage)


def own_runs(thread_id: UUID7) -> Select[tuple[str]]:
    """The runs in the history of the thread's own conversations — its agents', not
    the subagents' those agents spawned. A fork's history holds the runs it was
    forked with, so those are its own too."""
    return (
        select(ConversationRun["run_id"])
        .join(Conversation, Conversation["id"] == ConversationRun["conversation_id"])
        .where(
            Conversation["thread_id"] == thread_id, Conversation["subagent_id"] == ""
        )
    )


class ConversationNotFound(ValueError):
    """The requested conversation no longer exists or was never created."""


class ConversationManager(Manager, Locks[ConversationKey | CompactionSessionKey]):
    """Resolves and persists agent `Conversation` entities, and owns their model
    message history.

    A `Conversation` is one agent's model context within a `Thread` — not a human
    chat log; the user-facing chat ledger is `ThreadManager`'s `ThreadMessage`
    rows. A thread owns one conversation per agent, so the identity is
    `(thread_id, agent_tentacle_id, subagent_id)`: every sender in a group thread
    keys to the same owning agent's conversation, regardless of who woke it.
    """

    @validate_call
    async def get(
        self,
        conversation_id: UUID7,
        *,
        with_runs: bool = True,
        with_messages: bool = False,
    ) -> Conversation:
        """Read an existing conversation by id; raise on an unknown id.

        `with_runs` loads runs and their messages. Independently, `with_messages`
        loads effective context, cumulative usage, and deferred tool requests for
        an agent run, with upstream's persistence cursor. Disable both for a
        metadata-only read. Thread ownership is established by ensure().
        """
        options = [lazyload(Conversation["runs"])]
        if with_messages:
            options.extend(
                [
                    undefer(Conversation["usage"]),
                    undefer(Conversation["deferred_tool_requests"]),
                ]
            )
        async with async_session() as session:
            conversation = await session.get(
                Conversation,
                conversation_id,
                options=options,
            )
            if conversation is None:
                raise ConversationNotFound(f"unknown conversation {conversation_id}")
            if with_runs:
                await conversation.runs
        if with_messages:
            conversation.messages = await self.effective_messages(conversation)
            mark_stored(conversation, conversation.messages)
        return conversation

    @validate_call
    async def history(
        self, conversation_id: UUID7, *, limit: NonNegativeInt | None = None
    ) -> list[ModelRequest | ModelResponse]:
        """Read original run messages in order, including history replaced by compaction.

        A limit selects the most recent messages. Shared fork runs are included;
        compaction-owned replacements belong to get(..., with_messages=True).
        """
        statement = (
            select(ModelMessage)
            .select_from(Conversation)
            .join(Conversation["runs"])
            .join(AgentRun["messages"])
            .where(Conversation["id"] == conversation_id)
        )
        order = (AgentRun["started_at"], AgentRun["id"], ModelMessage["id"])
        if limit is not None:
            statement = statement.order_by(*(column.desc() for column in order)).limit(
                limit
            )
        else:
            statement = statement.order_by(*order)
        async with async_session() as session:
            # The polymorphic query anchor materializes its concrete message schemas.
            messages = list(
                cast(
                    Sequence[ModelRequest | ModelResponse],
                    (await session.scalars(statement)).all(),
                )
            )
        if limit is not None:
            messages.reverse()
        return messages

    @validate_call
    async def has_history(self, conversation_id: UUID7) -> bool:
        """Whether the conversation has any archived run messages."""
        async with async_session() as session:
            return bool(
                await session.scalar(
                    select(
                        select(ModelMessage["id"])
                        .select_from(Conversation)
                        .join(Conversation["runs"])
                        .join(AgentRun["messages"])
                        .where(Conversation["id"] == conversation_id)
                        .exists()
                    )
                )
            )

    async def effective_messages(
        self, conversation: Conversation
    ) -> list[ModelRequest | ModelResponse]:
        """Replay complete, ordered rewrites over the run archive.

        Each input must match the effective prefix at that boundary. Later run
        messages follow its replacement; unknown membership or order cannot be
        replayed safely and is refused rather than reviving the archive. Only
        message ids are replayed; bodies are loaded for the final context.
        """
        async with async_session() as session:
            linked = select(ConversationCompaction["compaction_id"]).where(
                ConversationCompaction["conversation_id"] == conversation.id
            )
            exists, unlinked = (
                await session.execute(
                    select(
                        select(Conversation["id"])
                        .where(Conversation["id"] == conversation.id)
                        .exists(),
                        select(Compaction["id"])
                        .where(
                            Compaction["conversation_id"] == conversation.id,
                            Compaction["id"].not_in(linked),
                        )
                        .exists(),
                    )
                )
            ).one()
            if not exists:
                raise ConversationNotFound(f"unknown conversation {conversation.id}")
            if unlinked:
                raise ValueError(
                    "effective context requires every compaction to be linked"
                )
            links = await session.list(
                ConversationCompaction,
                limit=None,
                expressions=[
                    ConversationCompaction["conversation_id"] == conversation.id
                ],
                order_bys=[ConversationCompaction["position"]],
            )
            if any(
                link.position != index for index, link in enumerate(links)
            ) or await session.scalar(
                select(
                    select(Compaction["id"])
                    .where(
                        Compaction["id"].in_(linked),
                        Compaction["membership_complete"].is_(False),
                    )
                    .exists()
                )
            ):
                raise ValueError(
                    "effective context requires complete, ordered compactions"
                )
            message_ids = deque(
                (
                    await session.scalars(
                        select(ModelMessage["id"])
                        .select_from(Conversation)
                        .join(Conversation["runs"])
                        .join(AgentRun["messages"])
                        .where(Conversation["id"] == conversation.id)
                        .order_by(
                            AgentRun["started_at"], AgentRun["id"], ModelMessage["id"]
                        )
                    )
                ).all()
            )
            members = (
                await session.list(
                    CompactionMembership,
                    limit=None,
                    expressions=[CompactionMembership["compaction_id"].in_(linked)],
                    order_bys=[
                        CompactionMembership["compaction_id"],
                        CompactionMembership["stage"],
                        CompactionMembership["position"],
                    ],
                )
                if links
                else []
            )
            stages: dict[UUID7, dict[CompactionStage, list[UUID7]]] = {
                link.compaction_id: {"before": [], "after": []} for link in links
            }
            for member in members:
                stages[member.compaction_id][member.stage].append(
                    member.model_message_id
                )
            for link in links:
                before = stages[link.compaction_id]["before"]
                if (
                    not before
                    or len(before) > len(message_ids)
                    or any(message_ids.popleft() != message_id for message_id in before)
                ):
                    raise ValueError(
                        "compaction input does not match the effective context"
                    )
                message_ids.extendleft(reversed(stages[link.compaction_id]["after"]))
            if not message_ids:
                return []
            # Arcanus resolves the abstract query anchor through its concrete mappings.
            rows = cast(
                Sequence[ModelRequest | ModelResponse],
                await session.list(
                    ModelMessage,
                    limit=None,
                    expressions=[ModelMessage["id"].in_(message_ids)],
                ),
            )
        by_id = {message.id: message for message in rows}
        return [by_id[message_id] for message_id in message_ids]

    async def record_context_compaction(
        self,
        conversation: Conversation,
        *,
        before: list[ModelRequest | ModelResponse],
        after: list[ModelRequest | ModelResponse],
        trigger: CompactionTrigger,
        happened_at: AwareDatetime | None = None,
    ) -> Compaction | None:
        """Persist an exact history rewrite after its original run messages exist.

        Retained inputs keep their ids and ownership. New replacement messages
        belong to the compaction, whose two ordered stages include every input
        and output message. A later suffix already archived by the run is allowed.
        The revision advances atomically; get(..., with_messages=True) before continuing.
        """
        before_ids = [message.id for message in before]
        after_ids = [message.id for message in after]
        if before_ids == after_ids:
            return None
        if (
            not before_ids
            or len(set(before_ids)) != len(before_ids)
            or len(set(after_ids)) != len(after_ids)
        ):
            raise ValueError(
                "compaction requires nonempty input and unique stage members"
            )
        async with self.lock(conversation.key), async_session() as session:
            current = await session.one(
                Conversation,
                expressions=[Conversation["id"] == conversation.id],
                options=[lazyload(Conversation["runs"])],
            )
            effective = await self.effective_messages(conversation)
            if [message.id for message in effective[: len(before)]] != before_ids:
                raise ValueError(
                    "compaction input does not match the effective context"
                )
            replacements = [
                message for message in after if message.id not in before_ids
            ]
            if any(
                message.run_id is not None or message.compaction_id is not None
                for message in replacements
            ):
                raise ValueError("replacement messages must not already have an owner")
            for message in replacements:
                message.conversation_id = str(conversation.id)
            compaction = Compaction(
                conversation_id=conversation.id,
                trigger=trigger,
                happened_at=happened_at,
                membership_complete=True,
                messages=RelationCollection(replacements),
            )
            current.revision = str(uuid7())
            await session.flush()
            position = await session.count(
                ConversationCompaction,
                expressions=[
                    ConversationCompaction["conversation_id"] == conversation.id
                ],
            )
            session.add(compaction)
            await session.flush()
            stages: tuple[tuple[CompactionStage, list[UUID7]], ...] = (
                ("before", before_ids),
                ("after", after_ids),
            )
            session.add_all(
                CompactionMembership(
                    compaction_id=compaction.id,
                    stage=stage,
                    position=index,
                    model_message_id=message_id,
                )
                for stage, message_ids in stages
                for index, message_id in enumerate(message_ids)
            )
            session.add(
                ConversationCompaction(
                    conversation_id=conversation.id,
                    compaction_id=compaction.id,
                    position=position,
                )
            )
            await session.commit()
            return compaction

    async def record_driven_compaction(
        self,
        conversation: Conversation,
        *,
        native_session_id: str,
        native_compaction_id: str,
        trigger: CompactionTrigger,
        happened_at: AwareDatetime | None,
        messages: list[ModelRequest | ModelResponse] | None = None,
    ) -> Compaction | None:
        """Persist a completed native item once, without inventing unavailable content."""
        async with (
            self.lock(
                CompactionSessionKey(
                    conversation.agent_tentacle_id,
                    native_session_id,
                )
            ),
            self.lock(conversation.key),
            async_session() as session,
        ):
            expressions = [
                Compaction["conversation_id"].in_(
                    select(Conversation["id"]).where(
                        Conversation["agent_tentacle_id"]
                        == conversation.agent_tentacle_id
                    )
                ),
                Compaction["native_session_id"] == native_session_id,
                Compaction["native_compaction_id"] == native_compaction_id,
            ]
            existing = await session.one_or_none(
                Compaction,
                expressions=expressions,
                options=[selectinload(Compaction["messages"])] if messages else [],
            )
            if existing is not None:
                if messages and not existing.messages:
                    existing.messages.extend(messages)
                if existing.trigger == "unknown":
                    existing.trigger = trigger
                if existing.happened_at is None:
                    existing.happened_at = happened_at
                await session.commit()
                return None
            compaction = Compaction(
                conversation_id=conversation.id,
                native_session_id=native_session_id,
                native_compaction_id=native_compaction_id,
                trigger=trigger,
                happened_at=happened_at,
                messages=RelationCollection(messages or []),
            )
            session.add(compaction)
            await session.commit()
            return compaction

    async def record_external_compaction(
        self,
        conversation: Conversation,
        *,
        native_session_id: str,
        source: str,
        start_offset: int | None,
        end_offset: int | None,
        happened_at: AwareDatetime,
        messages: list[ModelRequest | ModelResponse],
        native_compaction_id: str | None = None,
        trigger: CompactionTrigger = "unknown",
    ) -> ExternalCompaction | None:
        """Record one completed transcript observation, including driven sessions.

        Replaying the same byte range is a no-op. An active runtime binding owns
        the observation when present; otherwise the native conversation does.
        """
        async with (
            self.lock(
                CompactionSessionKey(conversation.agent_tentacle_id, native_session_id)
            ),
            self.lock(conversation.key),
            async_session() as session,
        ):
            coordinates = and_(
                ExternalCompaction["source"] == source,
                ExternalCompaction["start_offset"] == start_offset,
                ExternalCompaction["end_offset"] == end_offset,
            )
            identity = coordinates
            if native_compaction_id is not None:
                identity = Compaction["native_compaction_id"] == native_compaction_id
                if start_offset is not None and end_offset is not None:
                    identity = or_(identity, coordinates)
            expressions = [
                Compaction["conversation_id"].in_(
                    select(Conversation["id"]).where(
                        Conversation["agent_tentacle_id"]
                        == conversation.agent_tentacle_id
                    )
                ),
                Compaction["native_session_id"] == native_session_id,
                identity,
            ]
            if native_compaction_id is None and await session.scalar(
                select(select(Compaction["id"]).where(*expressions).exists())
            ):
                return None
            matches = (
                await session.list(
                    Compaction,
                    expressions=expressions,
                    order_bys=[Compaction["recorded_at"], Compaction["id"]],
                    options=[selectinload(Compaction["messages"])],
                )
                if native_compaction_id is not None
                else []
            )
            if not matches:
                owner = None
                if not conversation.subagent_id:
                    owner = await session.one_or_none(
                        Conversation,
                        expressions=[
                            Conversation["external_id"] == native_session_id,
                            Conversation["agent_tentacle_id"]
                            == conversation.agent_tentacle_id,
                            Conversation["id"] != conversation.id,
                            Conversation["subagent_id"] == "",
                        ],
                        options=[
                            noload(Conversation["runs"]),
                        ],
                    )
                compaction = ExternalCompaction(
                    conversation_id=owner.id if owner is not None else conversation.id,
                    native_session_id=native_session_id,
                    native_compaction_id=native_compaction_id,
                    trigger=trigger,
                    source=source,
                    start_offset=start_offset,
                    end_offset=end_offset,
                    happened_at=happened_at,
                    messages=RelationCollection(messages),
                )
                session.add(compaction)
                await session.commit()
                return compaction
            existing = matches[0]
            for duplicate in matches[1:]:
                if not existing.messages:
                    content = list(duplicate.messages)
                    duplicate.messages.clear()
                    existing.messages.extend(content)
                if existing.trigger == "unknown":
                    existing.trigger = duplicate.trigger
                if existing.happened_at is None:
                    existing.happened_at = duplicate.happened_at
                await session.delete(duplicate)
            if existing.native_compaction_id not in {None, native_compaction_id}:
                raise ValueError(
                    "Compaction coordinates identify a different native item"
                )
            existing.native_compaction_id = native_compaction_id
            add_messages = bool(messages) and not existing.messages
            if add_messages:
                existing.messages.extend(messages)
            if existing.happened_at is None:
                existing.happened_at = happened_at
            if existing.trigger == "unknown":
                existing.trigger = trigger
            if (
                add_messages
                and isinstance(existing, ExternalCompaction)
                and existing.source == source
            ):
                existing.end_offset = end_offset
            await session.commit()
            return None

    async def latest_model(self, conversation_id: UUID7) -> str | None:
        """Read the last reported model without loading the conversation's history."""
        async with async_session() as session:
            return await session.scalar(
                select(AgentRun["model_name"])
                .where(
                    AgentRun["id"].in_(
                        select(ConversationRun["run_id"]).where(
                            ConversationRun["conversation_id"] == conversation_id
                        )
                    ),
                    AgentRun["model_name"].is_not(None),
                )
                .order_by(AgentRun["started_at"].desc(), AgentRun["id"].desc())
                .limit(1)
            )

    async def latest_context_window(self, conversation_id: UUID7) -> int | None:
        """The latest run's capacity, including None when its route reported none."""
        async with async_session() as session:
            return await session.scalar(
                select(AgentRun["context_window"])
                .where(
                    AgentRun["id"].in_(
                        select(ConversationRun["run_id"]).where(
                            ConversationRun["conversation_id"] == conversation_id
                        )
                    )
                )
                .order_by(AgentRun["started_at"].desc(), AgentRun["id"].desc())
                .limit(1)
            )

    async def ensure(
        self,
        thread_id: UUID7,
        *,
        agent_tentacle_id: str,
        subagent_id: str = "",
        parent_conversation_id: UUID7 | None = None,
        with_runs: bool = True,
        native: bool = False,
    ) -> Conversation:
        """Resolve the conversation owned by `agent_tentacle_id` in `thread_id`,
        creating it if it does not yet exist. The owning agent is part of the
        identity — two agents in the same thread keep separate conversations — so callers must always
        supply it. `subagent_id` narrows to one subagent's own context; empty is
        the agent's long-lived conversation in the thread. A subagent context
        names the conversation that spawned it, and only a subagent context may.
        `with_runs` loads the run relationship, including each run's messages.
        Disable it when only identity, permissions, or the native resume id is
        needed. Effective context is loaded through get(..., with_messages=True).

        Native ingest sets `native=True` to resolve its uploaded thread's context.
        A native context belongs to one configured agent; another owner is refused.
        The thread's persisted native kind remains unchanged.
        """
        if bool(subagent_id) != (parent_conversation_id is not None):
            raise ValueError(
                "a subagent conversation requires both subagent_id and "
                "parent_conversation_id; a bare conversation takes neither"
            )
        # Serialize first sightings so the loser reads the committed row.
        async with async_session() as session:
            expressions = [
                Conversation["thread_id"] == thread_id,
                Conversation["subagent_id"] == subagent_id,
            ]
            if not native:
                expressions.append(
                    Conversation["agent_tentacle_id"] == agent_tentacle_id
                )
            else:
                thread = await session.get(Thread, thread_id)
                if thread is None or thread.kind != "native_thread":
                    raise ValueError("Native ingest requires a tailed thread")
            # Native claims share a lock across agents because the context has one owner.
            async with self.lock(
                ConversationKey(
                    thread_id=thread_id,
                    agent_id="" if native else agent_tentacle_id,
                    subagent_id=subagent_id,
                )
            ):
                conversation = await session.one_or_none(
                    Conversation,
                    options=[
                        lazyload(Conversation["runs"]),
                    ],
                    expressions=expressions,
                )
                if (
                    native
                    and conversation is not None
                    and conversation.agent_tentacle_id != agent_tentacle_id
                ):
                    raise ValueError("The tailed thread belongs to another agent")
                if conversation is None:
                    conversation = Conversation(
                        thread_id=thread_id,
                        agent_tentacle_id=agent_tentacle_id,
                        subagent_id=subagent_id,
                        parent_conversation_id=parent_conversation_id,
                    )
                    session.add(conversation)
                await session.flush()
                await session.commit()
            if with_runs:
                await conversation.runs
            return conversation

    async def link_parent_run(
        self,
        run_id: str,
        *,
        parent_run_id: str | None,
        parent_tool_call_id: str | None,
    ) -> None:
        """Stamp a recorded run's place in the run tree, after the fact. The
        runner stays ignorant of why it ran; the caller that spawned it links
        the turn to its own run and tool call once the child returns. MCP callers
        supply only the tool call id; saving the parent's tool result supplies
        its run id. A run that recorded nothing has no row to link — not an error.
        """
        async with async_session() as session:
            run = await session.one_or_none(
                AgentRun, expressions=[AgentRun["id"] == run_id]
            )
            if run is None:
                return
            run.parent_run_id = parent_run_id
            run.parent_tool_call_id = parent_tool_call_id
            await session.commit()

    async def subagents(self, parent_conversation_id: UUID7) -> list[Conversation]:
        """The subagent conversations spawned from `parent_conversation_id` — the
        live accomplices a `whisper` can reach. Rows only; callers resolve a
        chosen one through `ensure`."""
        async with async_session() as session:
            rows = await session.list(
                Conversation,
                limit=None,
                expressions=[
                    Conversation["parent_conversation_id"] == parent_conversation_id
                ],
            )
        return list(rows)

    async def for_thread(self, thread_id: UUID7) -> list[Conversation]:
        """The thread's agent conversations, subagents included, each with its runs.

        Run messages are not loaded: a reader after the messages pages through
        them with `messages_page`.
        """
        async with async_session() as session:
            rows = await session.list(
                Conversation,
                limit=None,
                options=[
                    selectinload(Conversation["runs"]).noload(AgentRun["messages"]),
                ],
                expressions=[Conversation["thread_id"] == thread_id],
            )
            for conversation in rows:
                await conversation.runs
        return list(rows)

    async def messages_page(
        self, thread_id: UUID7, cursor: str | None = None, *, limit: int
    ) -> Page[ModelRequest | ModelResponse]:
        """The model messages of the thread's own conversations, newest first, a
        page at a time and continued from an Arcanus `cursor`. Ids are uuid7 and a
        run's messages are appended in processing order, so id order preserves
        each transcript's sequence across committed turns.

        Each carries the chat rows bound to it — the ones a request was built from,
        the ones a response was delivered as — so a reader has who said what
        without the prompt's dressing. A subagent's messages stay out: they are its
        own conversation's, and the spawn call in its parent's run stands for them.
        """
        scope = ModelMessage["run_id"].in_(own_runs(thread_id))
        bookmark = None if cursor is None else Cursor[ModelMessage](cursor).bookmark
        after = (
            []
            if bookmark is None or bookmark.expression is None
            else [bookmark.expression]
        )
        newest_first = (ModelMessage["id"].desc(),)
        async with async_session() as session:
            rows = await session.list(
                ModelMessage,
                limit=limit + 1,
                order_bys=newest_first,
                options=[
                    selectinload(ModelMessage["thread_messages"]).noload(
                        ThreadMessage["model_messages"]
                    )
                ],
                expressions=[scope, *after],
            )
            total = await session.count(ModelMessage, expressions=[scope])
            # The polymorphic read materializes every row as one of the two.
            items = tuple(
                row
                for row in rows[:limit]
                if isinstance(row, ModelRequest | ModelResponse)
            )
            for item in items:
                await item.thread_messages
        last = ModelMessage["id"] < items[-1].id if items else None
        return Page(
            items=items,
            total=total,
            next_cursor=str(
                Cursor[ModelMessage].from_expressions(
                    bookmark=last, order_bys=newest_first, limit=limit
                )
            ),
            has_more=len(rows) > limit,
        )

    async def usage(self, thread_id: UUID7) -> RequestUsage:
        """The tokens the thread's own conversations spent, every response's usage
        summed. A subagent's are left to its own conversation, as its messages are.
        Only the usage column is read: summing a thread must not load its parts."""
        async with async_session() as session:
            usages = await session.scalars(
                select(ModelResponse["usage"]).where(
                    ModelResponse["run_id"].in_(own_runs(thread_id)),
                    ModelResponse["usage"].is_not(None),
                )
            )
            return sum(
                map(request_usage_adapter.validate_python, usages),
                RequestUsage(),
            )

    async def thread_id(self, conversation_id: UUID7) -> UUID7 | None:
        async with async_session() as session:
            conversation = await session.get(Conversation, conversation_id)
        if conversation is None:
            return None
        return conversation.thread_id

    async def record_agent_run(
        self,
        conversation: Conversation,
        run_id: str,
        messages: Sequence[PydanticModelMessage],
        *,
        name: str | None = None,
        model_name: str | None = None,
        context_window: int | None = None,
        context_used: int | None = None,
        permission_mode: AgentPermissionMode | None = None,
        cwd: Path | None = None,
        external_id: str | None = None,
        native_turn_id: str | None = None,
        parent_run_id: str | None = None,
        parent_tool_call_id: str | None = None,
    ) -> AgentRun | None:
        """Persist an agent run's archive, completing its registration if present.
        `cwd` is the directory the run happened in, None when its caller has none.
        `external_id`, when given, updates the conversation's resumable agent
        session handle in the same commit (external-runtime agents own their own
        session). The parent pair marks a subagent's turn: the run whose tool
        call spawned it, and which call it answers."""
        if not messages:
            return None
        async with async_session() as session:
            run = await session.one_or_none(
                AgentRun,
                expressions=[
                    AgentRun["id"] == run_id,
                    AgentRun["metadata"].is_not(None),
                    ~AgentRun["messages"].any(),
                ],
                options=[noload(AgentRun["messages"])],
            )
        if run is None:
            run = AgentRun(
                id=run_id,
                conversation_id=conversation.id,
                parent_run_id=parent_run_id,
                started_at=messages[0].timestamp,
            )
        elif run.conversation_id != conversation.id:
            raise ValueError("Run is registered to a different conversation")
        elif parent_run_id not in (None, run.parent_run_id):
            raise ValueError("Run is registered with a different parent")
        run.name = name
        run.model_name = model_name
        run.context_window = context_window
        run.context_used = context_used
        run.permission_mode = permission_mode
        run.cwd = cwd
        run.native_session_id = external_id
        run.native_turn_id = native_turn_id
        run.parent_tool_call_id = parent_tool_call_id
        run.revalidate()
        run.messages.extend(run.messages.bless([vars(m) for m in messages]))
        return await self.persist_run(run)

    async def driven_run(
        self, agent_tentacle_id: str, session_id: str, turn_id: str
    ) -> AgentRun | None:
        """Find the driven owner of a native turn without loading its messages."""
        async with async_session() as session:
            return await session.one_or_none(
                AgentRun,
                expressions=[
                    AgentRun["kind"] == "octomate",
                    AgentRun["conversation_id"].in_(
                        select(Conversation["id"]).where(
                            Conversation["agent_tentacle_id"] == agent_tentacle_id
                        )
                    ),
                    AgentRun["native_session_id"] == session_id,
                    AgentRun["native_turn_id"] == turn_id,
                ],
                options=[noload(AgentRun["messages"])],
            )

    async def record_external_run(
        self,
        conversation: Conversation,
        run_id: str,
        messages: Sequence[PydanticModelMessage] = (),
        *,
        name: str | None = None,
        model_name: str | None = None,
        context_window: int | None = None,
        context_used: int | None = None,
        permission_mode: AgentPermissionMode | None = None,
        cwd: Path | None = None,
        native_session_id: str,
        source: str | None = None,
        start_offset: int | None = None,
        end_offset: int | None = None,
        last_line_uuid: str | None = None,
        parent_run_id: str | None = None,
        parent_tool_call_id: str | None = None,
    ) -> ExternalAgentRun | None:
        """Persist a turn of a native runtime's session as the
        `external` variant. `cwd` is the directory the turn ran in, as the hook or
        the transcript reported it. `native_session_id` doubles as the
        conversation's resumable handle (`external_id`).

        A turn already recorded by the driven runtime keeps its original run and
        messages. Its owning agent, session and turn IDs let ingest skip the replay
        without copying it into the native ledger or saving transcript coordinates on it.

        The byte range is what marks a turn finished. A run carrying `end_offset` was
        assembled from the transcript and is final, so recording it again — a recovery
        overlapping a live tail, or a resume re-covering committed bytes — is a no-op
        returning `None` rather than a primary-key collision. That guard belongs here,
        at the durable sink: a caller's in-memory guard only knows the runs *it* wrote,
        never one another tailer already committed.

        Omitting messages reserves a run identity without changing an existing run.
        Native hooks use this so the transcript remains the only message writer.

        Completing a reserved run fills its messages in transcript order while
        preserving its identity. Older incomplete imports may contain provisional
        messages; the completed transcript replaces those in the same commit.
        """
        driven = await self.driven_run(
            conversation.agent_tentacle_id, native_session_id, run_id
        )
        if driven is not None:
            return None
        run = ExternalAgentRun(
            id=run_id,
            conversation_id=conversation.id,
            native_turn_id=run_id,
            name=name,
            model_name=model_name,
            context_window=context_window,
            context_used=context_used,
            permission_mode=permission_mode,
            cwd=cwd,
            parent_run_id=parent_run_id,
            parent_tool_call_id=parent_tool_call_id,
            started_at=messages[0].timestamp if messages else None,
            messages=[vars(m) for m in messages],  # pyright: ignore[reportArgumentType]
            native_session_id=native_session_id,
            source=source,
            start_offset=start_offset,
            end_offset=end_offset,
            last_line_uuid=last_line_uuid,
        )
        async with async_session() as session:
            stored = await session.one_or_none(
                AgentRun, expressions=[AgentRun["id"] == run_id]
            )
            if stored is not None:
                if not messages and end_offset is None:
                    return stored if isinstance(stored, ExternalAgentRun) else None
                if (
                    not isinstance(stored, ExternalAgentRun)
                    or stored.end_offset is not None
                ):
                    return None
                # `model_messages.run_id` is NOT NULL, so the sketch's messages are
                # deleted rather than left behind by the merged collection.
                sketch = list(stored.messages)
                run = await session.merge(run)
                for message in sketch:
                    await session.delete(message)
                owner = await session.get(
                    Conversation,
                    conversation.id,
                    options=[
                        noload(Conversation["runs"]),
                    ],
                )
                if owner is not None:
                    owner.external_id = native_session_id
                await session.commit()
                return run
        return await self.persist_run(run)

    async def persist_run(self, run: RunT) -> RunT:
        """Persist the supplied run and its messages in one transaction.
        Registered runs already have a conversation link.
        The run's native session ID updates the conversation's resumable handle
        in the same commit.

        Only the supplied run and its messages are added, never the caller's whole
        conversation graph: re-merging it (prior runs and their message
        collections) can make SQLAlchemy believe a message was dropped from a
        stale run collection and null its NOT NULL `run_id`, raising an
        IntegrityError.
        """
        async with async_session() as session:
            session.add(run)
            reloaded = await session.one_or_none(
                Conversation,
                expressions=[Conversation["id"] == run.conversation_id],
                options=[
                    noload(Conversation["runs"]),
                ],
            )
            if reloaded is not None:
                if run.metadata is None:
                    reloaded.runs.append(run)
                reloaded.external_id = run.native_session_id
            returned_calls = {
                part.tool_call_id
                for message in await run.messages
                if isinstance(message, ModelRequest)
                for part in message.parts
                if isinstance(part, ToolReturnPart)
            }
            if returned_calls:
                children = await session.list(
                    AgentRun,
                    limit=None,
                    expressions=[
                        AgentRun["parent_run_id"].is_(None),
                        AgentRun["parent_tool_call_id"].in_(returned_calls),
                        AgentRun["conversation_id"].in_(
                            select(Conversation["id"]).where(
                                Conversation["parent_conversation_id"]
                                == run.conversation_id
                            )
                        ),
                    ],
                )
                for child in children:
                    child.parent_run_id = run.id
            await session.commit()
            await run.messages
        return run

    async def fork(
        self,
        source: Conversation,
        target: Conversation,
        *,
        carry_external_id: bool = False,
        external_id: str | None = None,
        transcript: Jsonl | None = None,
        end_offset: int | None = None,
        permission_mode: AgentPermissionMode | None = None,
    ) -> str | None:
        """Continue `source`'s history in an empty `target`, in one transaction.

        Nothing is copied. The target's history points at the source's runs through
        `conversation_runs`, and its thread shows the source thread's ledgers through
        `thread_ledgers`, each frozen where it stands now: whatever either side says
        afterwards is its own. Only the selected conversation receives the
        independently forked runtime handle.

        A native import passes `end_offset`, where the transcript's last completed
        turn ends (an uploaded snapshot's size by default), to leave out the turns
        that end past it and cut the source's ledger before their chat.
        `carry_external_id` moves the existing handle instead, for runtimes whose
        continuation requires it. Return the id of the last run the target's history
        includes, if any.
        """
        if source.thread_id == target.thread_id:
            raise ValueError("A fork requires an independent destination thread")
        if transcript is not None and (external_id is None or carry_external_id):
            raise ValueError("An imported transcript requires an independent session")
        if external_id is not None and (
            carry_external_id or external_id == source.external_id
        ):
            raise ValueError(
                "a fork requires a new external id without carrying the source"
            )
        if end_offset is None and transcript is not None:
            end_offset = transcript.size

        async with async_session() as session:
            stored_target = await session.get(
                Conversation,
                target.id,
                options=[
                    noload(Conversation["runs"]),
                ],
            )
            if stored_target is None:
                raise ValueError(f"unknown conversation {target.id}")
            if stored_target.revision is not None:
                raise ValueError("fork target already holds a saved context")
            target_thread = await session.get(Thread, target.thread_id)
            if target_thread is None:
                raise ValueError(f"unknown thread {target.thread_id}")
            if await session.count(
                Conversation,
                expressions=[
                    Conversation["thread_id"] == target.thread_id,
                    or_(Conversation["id"] != target.id, Conversation["runs"].any()),
                ],
            ) or await session.count(
                Thread,
                expressions=[
                    Thread["id"] == target.thread_id,
                    Thread["messages"].any(ThreadMessage["actor_kind"] != "system"),
                ],
            ):
                raise ValueError(
                    "fork target already holds history; refusing to splice histories"
                )

            source_thread = await session.get(Thread, source.thread_id)
            if source_thread is None:
                raise ValueError(f"unknown thread {source.thread_id}")
            origin = await session.get(
                Conversation,
                source.id,
                options=[
                    undefer(Conversation["usage"]),
                    undefer(Conversation["deferred_tool_requests"]),
                    selectinload(Conversation["runs"]).noload(AgentRun["messages"]),
                ],
            )
            if origin is None:
                raise ValueError(f"unknown conversation {source.id}")
            if origin.revision is not None:
                if end_offset is not None:
                    raise ValueError(
                        "a current context cannot be cut at a transcript offset"
                    )
                stored_target.usage = origin.usage
                stored_target.deferred_tool_requests = origin.deferred_tool_requests
                stored_target.revision = str(uuid7())
                await session.execute(
                    insert(ConversationCompaction).from_select(
                        ["conversation_id", "compaction_id", "position"],
                        select(
                            literal(target.id),
                            ConversationCompaction["compaction_id"],
                            ConversationCompaction["position"],
                        ).where(ConversationCompaction["conversation_id"] == source.id),
                    )
                )
            dropped: set[str] = set()
            # Without an end offset the cut is the ledger's last message. With one,
            # it falls after the last chat of a completed turn: bound to one, or
            # neither bound to nor filed under an unfinished turn (a native ledger
            # names a turn's prompt by the turn's id), and said no later than the last
            # message the fork keeps.
            cut = select(func.max(ThreadMessage["id"])).where(
                ThreadMessage["thread_id"] == source.thread_id
            )
            if end_offset is not None:
                # A turn of the origin's own session that ends past the offset is
                # unfinished, and so is the subagent work it spawned. The offset is a
                # position in that session's transcript, so it judges no other run.
                unfinished = (
                    select(ExternalAgentRun["id"])
                    .where(
                        ExternalAgentRun["id"].in_(
                            select(ConversationRun["run_id"]).where(
                                ConversationRun["conversation_id"] == source.id
                            )
                        ),
                        ExternalAgentRun["native_session_id"] == origin.external_id,
                        or_(
                            ExternalAgentRun["end_offset"].is_(None),
                            ExternalAgentRun["end_offset"] > end_offset,
                        ),
                    )
                    .cte(recursive=True)
                )
                unfinished = unfinished.union(
                    select(AgentRun["id"]).where(
                        AgentRun["parent_run_id"] == unfinished.c.id
                    )
                )
                dropped = set(await session.scalars(select(unfinished.c.id)))
                # Every run in the history of a conversation in the source thread,
                # but the unfinished ones.
                kept = select(ConversationRun["run_id"]).where(
                    ConversationRun["conversation_id"].in_(
                        select(Conversation["id"]).where(
                            Conversation["thread_id"] == source.thread_id
                        )
                    ),
                    ConversationRun["run_id"].not_in(dropped),
                )
                bound = select(MessageBinding["thread_message_id"])
                cut = cut.where(
                    or_(
                        ThreadMessage["id"].in_(
                            bound.where(MessageBinding["run_id"].in_(kept))
                        ),
                        and_(
                            ThreadMessage["id"].not_in(
                                bound.where(MessageBinding["run_id"].in_(dropped))
                            ),
                            or_(
                                ThreadMessage["platform_message_id"].is_(None),
                                ThreadMessage["platform_message_id"].not_in(dropped),
                            ),
                            ThreadMessage["happened_at"]
                            <= select(func.max(ModelMessage["timestamp"]))
                            .where(ModelMessage["run_id"].in_(kept))
                            .scalar_subquery(),
                        ),
                    )
                )
            own_cut = await session.scalar(cut)
            ledgers = await session.list(
                ThreadLedger,
                limit=None,
                order_bys=[ThreadLedger["ledger_id"]],
                expressions=[ThreadLedger["thread_id"] == source.thread_id],
            )
            # Every ledger the source shows reaches the fork frozen: its own at the
            # cut, and the ones a fork froze into it where they already stop.
            cuts = {ledger.ledger_id: ledger.cut_message_id for ledger in ledgers}
            cuts[source.thread_id] = own_cut
            session.add_all(
                ThreadLedger(
                    thread_id=target.thread_id,
                    ledger_id=ledger_id,
                    cut_message_id=cut,
                )
                for ledger_id, cut in cuts.items()
                if cut is not None
            )

            await session.execute(
                insert(ConversationRun).from_select(
                    ["conversation_id", "run_id"],
                    select(literal(target.id), ConversationRun["run_id"]).where(
                        ConversationRun["conversation_id"] == source.id,
                        ConversationRun["run_id"].not_in(dropped),
                    ),
                )
            )
            # The cursor stops where the source's did, within what the fork shows.
            shown = max(
                (cut for cut in cuts.values() if cut is not None),
                default=None,
            )
            cursor = source_thread.source_cursor_message_id
            target_thread.source_cursor_message_id = (
                shown if cursor is None or shown is None else min(cursor, shown)
            )
            stored_target.name = origin.name
            stored_target.permission_mode = permission_mode or origin.permission_mode
            stored_target.effort = origin.effort
            stored_target.allowed_tools = list(origin.allowed_tools)
            if transcript is not None:
                session.add(transcript)
                await session.flush()
                stored_target.transcript_file_id = transcript.id
            if carry_external_id:
                stored_target.external_id = origin.external_id
                origin.external_id = None
            elif external_id is not None:
                stored_target.external_id = external_id
            await session.commit()
        if carry_external_id:
            source.external_id = None
        elif external_id is not None:
            target.external_id = external_id
        forked = [run.id for run in origin.runs if run.id not in dropped]
        return forked[-1] if forked else None

    async def store_transcript(
        self,
        uploaded: UploadFile,
        start: int,
        *,
        conversation: Conversation,
        files: FileManager,
        owner_id: UUID7,
    ) -> FileVariant:
        """Persist new bytes of a native conversation's owner-scoped transcript.

        Create the first file and conversation reference in one transaction;
        subsequent uploads append to that file. Never mutate the caller's conversation.
        """
        async with self.lock(conversation.key):
            current = await self.get(conversation.id, with_runs=False)
            previous_id = current.transcript_file_id
            if previous_id is not None:
                return await files.append(
                    previous_id, uploaded.file, offset=start, owner_id=owner_id
                )
            if start != 0:
                raise ValueError("The first transcript upload must start at zero")
            async with (
                files.upload(uploaded, owner_id=owner_id) as transcript,
                async_session() as session,
            ):
                session.add(transcript)
                await session.flush()
                stored = await session.get(
                    Conversation,
                    conversation.id,
                    options=[lazyload(Conversation["runs"])],
                )
                if stored is None:
                    raise ValueError(f"unknown conversation {conversation.id}")
                stored.transcript_file_id = transcript.id
                await session.commit()
            return transcript

    async def set_external_id(
        self, conversation: Conversation, external_id: str
    ) -> None:
        """Bind a conversation to its replacement runtime session."""
        async with async_session() as session:
            stored = await session.get(Conversation, conversation.id)
            if stored is None:
                raise ValueError(f"unknown conversation {conversation.id}")
            stored.external_id = external_id
            await session.commit()
        conversation.external_id = external_id

    async def set_permission_mode(
        self,
        conversation: Conversation,
        mode: AgentPermissionMode | None,
    ) -> Conversation:
        """Store the approval posture this conversation's agent works under.

        Callers selecting a mode validate against the running agent's catalog.
        Native observers also store modes that are no longer selectable. None clears
        the selection and lets the agent's configured default decide.

        This method only persists the selection. User changes go through the
        owning agent tentacle, which also updates its live runtime when supported.
        """
        async with async_session() as session:
            stored = await session.get(Conversation, conversation.id)
            if stored is None:
                raise ValueError(f"unknown conversation {conversation.id}")
            stored.permission_mode = mode
            await session.commit()
        conversation.permission_mode = mode
        return conversation

    async def set_effort(
        self, conversation: Conversation, effort: str | None
    ) -> Conversation:
        """Store the reasoning effort this conversation's runs ask for.

        Callers validate the level against the route the conversation runs on;
        None clears it and lets the runtime's own default decide.
        """
        async with async_session() as session:
            stored = await session.get(Conversation, conversation.id)
            if stored is None:
                raise ValueError(f"unknown conversation {conversation.id}")
            stored.effort = effort
            await session.commit()
        conversation.effort = effort
        return conversation

    async def set_name(self, conversation: Conversation, name: str) -> Conversation:
        """Store the name the runtime running this session grabbed for itself.

        Only a runtime that names its own sessions has one to store, and it revises
        it as the session goes on, so this overwrites. A name with nothing in it is
        not one, and leaves the conversation as it was.
        """
        named = name.strip()
        if not named or conversation.name == named:
            return conversation
        async with async_session() as session:
            stored = await session.get(Conversation, conversation.id)
            if stored is None:
                raise ValueError(f"unknown conversation {conversation.id}")
            stored.name = named
            await session.commit()
        conversation.name = named
        return conversation

    async def grant_session_tool(
        self,
        conversation: Conversation,
        tool_name: str,
    ) -> None:
        """Persist an `allow for session` grant on the conversation, so the tool
        auto-approves on later turns."""
        fresh = await self.ensure(
            conversation.thread_id,
            agent_tentacle_id=conversation.agent_tentacle_id,
        )
        if tool_name in fresh.allowed_tools:
            return
        async with async_session() as session:
            stored = await session.get(Conversation, fresh.id)
            if stored is None:
                return
            stored.allowed_tools = [*stored.allowed_tools, tool_name]
            await session.commit()

    async def search_messages(
        self,
        conversation_id: UUID7,
        query: str,
        *,
        role: Literal["user", "assistant"] | None = None,
        limit: int = 10,
    ) -> list[ModelMessage]:
        """Conversation messages whose `message_text` contains `query`
        (case-insensitive), ordered chronologically. One polymorphic select spans
        both kinds, so an unfiltered search returns user and assistant hits in a
        single pass; `role` narrows it when set. Tool-call/thinking messages carry
        no `message_text` and never match — reach them via
        `messages_before`/`messages_after`."""
        expressions = [
            ModelMessage["conversation_id"] == str(conversation_id),
            ModelMessage["message_text"].ilike(f"%{query}%"),
        ]
        if role is not None:
            expressions.append(ModelMessage["role"] == role)
        async with async_session() as session:
            rows = await session.list(
                ModelMessage,
                limit=limit,
                order_bys=[ModelMessage["id"]],
                expressions=expressions,
            )
        return list(rows)

    async def messages_before(
        self,
        conversation_id: UUID7,
        anchor_id: UUID7,
        *,
        limit: int = 5,
    ) -> list[ModelMessage]:
        """The `limit` messages immediately preceding `anchor_id` in the
        conversation (all kinds), oldest-first."""
        async with async_session() as session:
            rows = await session.list(
                ModelMessage,
                limit=limit,
                order_bys=[ModelMessage["id"].desc()],
                expressions=[
                    ModelMessage["conversation_id"] == str(conversation_id),
                    ModelMessage["id"] < anchor_id,
                ],
            )
        return list(reversed(rows))

    async def messages_after(
        self,
        conversation_id: UUID7,
        anchor_id: UUID7,
        *,
        limit: int = 5,
    ) -> list[ModelMessage]:
        """The `limit` messages immediately following `anchor_id` in the
        conversation (all kinds), oldest-first."""
        async with async_session() as session:
            rows = await session.list(
                ModelMessage,
                limit=limit,
                order_bys=[ModelMessage["id"]],
                expressions=[
                    ModelMessage["conversation_id"] == str(conversation_id),
                    ModelMessage["id"] > anchor_id,
                ],
            )
        return list(rows)

    async def related_chat_messages(
        self,
        model_message_id: UUID7,
    ) -> list[ThreadMessage]:
        async with async_session() as session:
            message = await session.one_or_none(
                ModelMessage,
                options=[selectinload(ModelMessage["thread_messages"])],
                expressions=[ModelMessage["id"] == model_message_id],
            )
            if message is None:
                return []
            return [
                ThreadMessage.model_validate(thread_message)
                for thread_message in message.thread_messages
            ]


@dataclass
class ConversationStore(PydanticConversationStore):
    """Upstream conversation bookkeeping over the shared conversation manager.

    Loading delegates to the manager; record_conversation appends run messages and
    carryover state through Arcanus. Linked-entry persistence is not implemented,
    so upstream save, branch, fork, listing, and deletion remain unavailable.
    """

    manager: ConversationManager
    conversation_id_adapter: ClassVar[TypeAdapter[UUID7]] = TypeAdapter(UUID7)

    async def load(
        self,
        conversation_id: str,
        *,
        limit: int | None = None,
        scope: str | None = None,
    ) -> Conversation | None:
        if limit is not None or scope is not None:
            raise NotImplementedError(
                "Scoped and windowed conversation loading is not implemented"
            )
        identity = self.conversation_id_adapter.validate_python(conversation_id)
        try:
            return await self.manager.get(identity, with_runs=False, with_messages=True)
        except ConversationNotFound:
            return None

    @validate_call
    async def record_conversation(
        self,
        conversation: PydanticConversation,
    ) -> list[ModelRequest | ModelResponse]:
        """Append new run messages and replace carryover state in one transaction.

        Follows upstream load/save bookkeeping, returning the new archive rows
        instead of wrapping them in ConversationEntry objects. Runs retain their
        ownership; compaction replacements must be recorded through
        manager.record_context_compaction before loading or saving the context.
        This store does not treat an unrecorded rewrite as an implicit branch.
        Keep the passed conversation to retain its updated revision and cursor.
        """
        conversation_id = self.conversation_id_adapter.validate_python(
            conversation.conversation_id
        )
        if conversation.new_message_index is not None and not (
            0 <= conversation.new_message_index <= len(conversation.messages)
        ):
            raise ValueError("new_message_index is outside the conversation history")
        boundary = stored_boundary(conversation)
        revision = str(uuid7())
        saved: list[ModelRequest | ModelResponse] = []
        links: list[ConversationRun] = []
        async with async_session() as session:
            current = await session.get(
                Conversation,
                conversation_id,
                options=[lazyload(Conversation["runs"])],
            )
            if current is None or current.revision != conversation.revision:
                raise ConversationConflict(str(conversation_id))
            if boundary is None:
                history = await self.manager.effective_messages(current)
                if conversation.new_message_index is not None:
                    history = history[len(history) - conversation.new_message_index :]
                stored = ModelMessagesTypeAdapter.validate_python(
                    [vars(message) for message in history]
                )
                if conversation.messages[: len(stored)] != stored:
                    raise ValueError(
                        "record context compaction before saving rewritten history"
                    )
                boundary = len(stored)
            pending = conversation.messages[boundary:]
            by_run: dict[str, list[PydanticModelMessage]] = {}
            for message in pending:
                if message.run_id is None:
                    raise ValueError("new conversation messages require a run owner")
                if message.conversation_id not in (None, str(conversation_id)):
                    raise ValueError("message belongs to a different conversation")
                by_run.setdefault(message.run_id, []).append(message)
            current.usage = conversation.usage
            current.deferred_tool_requests = conversation.deferred_tool_requests
            current.revision = revision
            try:
                await session.flush()
            except StaleDataError as error:
                raise ConversationConflict(str(conversation_id)) from error
            runs = (
                {
                    run.id: run
                    for run in await session.list(
                        AgentRun,
                        limit=None,
                        expressions=[AgentRun["id"].in_(by_run)],
                        options=[noload(AgentRun["messages"])],
                    )
                }
                if by_run
                else {}
            )
            for run_id, messages in by_run.items():
                run = runs.get(run_id)
                if run is None:
                    run = AgentRun(
                        id=run_id,
                        conversation_id=conversation_id,
                        started_at=messages[0].timestamp,
                    )
                    session.add(run)
                    links.append(
                        ConversationRun(conversation_id=conversation_id, run_id=run_id)
                    )
                elif run.conversation_id != conversation_id or isinstance(
                    run, ExternalAgentRun
                ):
                    raise ValueError(
                        "run belongs to a different conversation or harness"
                    )
                rows = run.messages.bless([vars(message) for message in messages])
                for row in rows:
                    row.conversation_id = str(conversation_id)
                run.messages.extend(rows)
                saved.extend(rows)
            if links:
                await session.flush()
                session.add_all(links)
            await session.commit()
        conversation.revision = revision
        mark_stored(conversation, conversation.messages)
        return saved

    async def read_record(self, conversation_id: str) -> ConversationRecord | None:
        raise NotImplementedError("Conversation record heads are not persisted yet")

    async def read_records(
        self, *, scope: str | None = None, limit: int | None = None
    ) -> list[ConversationRecord]:
        raise NotImplementedError("Conversation record listing is not implemented")

    async def write_record(
        self,
        record: ConversationRecord,
        *,
        new_entries: Sequence[ConversationEntry] = (),
        expected_revision: str | None,
    ) -> None:
        raise NotImplementedError("Linked conversation entries are not persisted yet")

    async def delete_record(
        self, conversation_id: str, *, expected_revision: str
    ) -> None:
        raise NotImplementedError(
            "Revision-checked conversation record deletion is not implemented"
        )

    async def read_entry(self, entry_id: str) -> ConversationEntry:
        raise NotImplementedError("Linked conversation entries are not persisted yet")
