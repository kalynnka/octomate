"""Agent conversations and their model message history."""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import Literal, TypeVar

from arcanus.materia.sqlalchemy import lazyload, noload, selectinload
from fastapi import UploadFile
from pydantic import UUID7
from pydantic_ai.messages import ModelMessage as PydanticModelMessage
from pydantic_ai.messages import ToolCallPart
from sqlalchemy import and_, func, insert, literal, or_, select

from octomate.database import async_session
from octomate.managers.base import Locks, Manager
from octomate.managers.files import FileManager
from octomate.schemas.conversation import Conversation, ConversationRun
from octomate.schemas.files import FileVariant, Jsonl
from octomate.schemas.messages import ModelMessage, ModelResponse
from octomate.schemas.runs import AgentRun, ExternalAgentRun
from octomate.schemas.thread import (
    MessageBinding,
    Thread,
    ThreadLedger,
    ThreadMessage,
)
from octomate.types.permissions import AgentPermissionMode

RunT = TypeVar("RunT", bound=AgentRun)


class ConversationManager(Manager, Locks[tuple[UUID7, str, str]]):
    """Resolves and persists agent `Conversation` entities, and owns their model
    message history.

    A `Conversation` is one agent's model context within a `Thread` — not a human
    chat log; the user-facing chat ledger is `ThreadManager`'s `ThreadMessage`
    rows. A thread owns one conversation per agent, so the identity is
    `(thread_id, agent_tentacle_id, subagent_id)`: every sender in a group thread
    keys to the same owning agent's conversation, regardless of who woke it.
    """

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

    async def ensure(
        self,
        thread_id: UUID7,
        *,
        agent_tentacle_id: str,
        subagent_id: str = "",
        parent_conversation_id: UUID7 | None = None,
        with_history: bool = True,
    ) -> Conversation:
        """Resolve the conversation owned by `agent_tentacle_id` in `thread_id`,
        creating it if it does not yet exist. Its `conversation.messages` is the
        live model history. The owning agent is part of the identity — two agents
        in the same thread keep separate conversations — so callers must always
        supply it. `subagent_id` narrows to one subagent's own context; empty is
        the agent's long-lived conversation in the thread. A subagent context
        names the conversation that spawned it, and only a subagent context may.
        `with_history=False` leaves runs and messages unloaded for callers that
        only need identity, permissions, or the native session's resume id."""
        if bool(subagent_id) != (parent_conversation_id is not None):
            raise ValueError(
                "a subagent conversation requires both subagent_id and "
                "parent_conversation_id; a bare conversation takes neither"
            )
        # Serialize first sightings so the loser reads the committed row.
        async with async_session() as session:
            async with self.lock((thread_id, agent_tentacle_id, subagent_id)):
                conversation = await session.one_or_none(
                    Conversation,
                    options=[
                        lazyload(Conversation["runs"]),
                        lazyload(Conversation["messages"]),
                    ],
                    expressions=[
                        Conversation["thread_id"] == thread_id,
                        Conversation["agent_tentacle_id"] == agent_tentacle_id,
                        Conversation["subagent_id"] == subagent_id,
                    ],
                )
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
            if with_history:
                await conversation.runs
                await conversation.messages
            return conversation

    async def get(
        self, conversation_id: UUID7, *, with_history: bool = True
    ) -> Conversation:
        """Resolve a conversation by id — one fresh read; raises on an unknown
        id. This is the by-id path for a run addressed at a pre-ensured
        conversation (a commissioned accomplice's child context): the caller
        ensured it and owns any thread/agent validation. `with_history=False`
        leaves runs and messages unloaded, as on `ensure`."""
        if not with_history:
            options = [
                lazyload(Conversation["runs"]),
                lazyload(Conversation["messages"]),
            ]
        else:
            options = []
        async with async_session() as session:
            conversation = await session.get(
                Conversation,
                conversation_id,
                options=options,
            )
            if conversation is None:
                raise ValueError(f"unknown conversation {conversation_id}")
            if with_history:
                await conversation.runs
                await conversation.messages
        return conversation

    async def link_parent_run(
        self,
        run_id: str,
        *,
        parent_run_id: str,
        parent_tool_call_id: str | None,
    ) -> None:
        """Stamp a recorded run's place in the run tree, after the fact. The
        runner stays ignorant of why it ran; the caller that spawned it links
        the turn to its own run and tool call once the child returns. A run
        that recorded nothing has no row to link — not an error."""
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

    async def for_thread(
        self, thread_id: UUID7, *, with_run_messages: bool = False
    ) -> list[Conversation]:
        """The thread's agent conversations, subagents included, each with its runs.

        Both message relations here are lazy="selectin", so the plain query would read
        every model message the thread ever produced twice over — once under the
        conversation and once under the run that owns it. The conversation's copy is
        always dropped; the run's is what `with_run_messages` asks for, and it is the
        only place the thinking and the tool calls are, so a reader rebuilding a
        thread's middle needs it and a reader listing conversations does not.
        """
        runs = selectinload(Conversation["runs"])
        async with async_session() as session:
            rows = await session.list(
                Conversation,
                limit=None,
                options=[
                    noload(Conversation["messages"]),
                    runs if with_run_messages else runs.noload(AgentRun["messages"]),
                ],
                expressions=[Conversation["thread_id"] == thread_id],
            )
        return list(rows)

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
        permission_mode: AgentPermissionMode | None = None,
        cwd: Path | None = None,
        external_id: str | None = None,
        native_id: str | None = None,
        native_turn_id: str | None = None,
        parent_run_id: str | None = None,
        parent_tool_call_id: str | None = None,
    ) -> AgentRun | None:
        """Persist a fresh agent run.
        `cwd` is the directory the run happened in, None when its caller has none.
        `external_id`, when given, updates the conversation's resumable agent
        session handle in the same commit (external-runtime agents own their own
        session). The parent pair marks a subagent's turn: the run whose tool
        call spawned it, and which call it answers."""
        if not messages:
            return None
        run = AgentRun(
            id=run_id,
            conversation_id=conversation.id,
            native_id=native_id,
            native_session_id=external_id,
            native_turn_id=native_turn_id,
            name=name,
            model_name=model_name,
            permission_mode=permission_mode,
            cwd=cwd,
            parent_run_id=parent_run_id,
            parent_tool_call_id=parent_tool_call_id,
            started_at=messages[0].timestamp,
            # Shallow `vars(m)` dicts, so pydantic routes each through the blessed
            # `ModelRequest | ModelResponse` union at construction — the raw pydantic-ai
            # dataclass is rejected, not being an instance of our blessed subclass.
            messages=[vars(m) for m in messages],  # pyright: ignore[reportArgumentType]
        )
        return await self.persist_run(
            run, conversation_id=conversation.id, external_id=external_id
        )

    async def driven_run(
        self, native_id: str, session_id: str, turn_id: str
    ) -> AgentRun | None:
        """Find the driven owner of a native turn without loading its messages."""
        async with async_session() as session:
            return await session.one_or_none(
                AgentRun,
                expressions=[
                    AgentRun["kind"] == "octomate",
                    AgentRun["native_id"] == native_id,
                    AgentRun["native_session_id"] == session_id,
                    AgentRun["native_turn_id"] == turn_id,
                ],
                options=[noload(AgentRun["messages"])],
            )

    async def record_external_run(
        self,
        conversation: Conversation,
        run_id: str,
        messages: Sequence[PydanticModelMessage],
        *,
        name: str | None = None,
        model_name: str | None = None,
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
        """Persist a turn of an external runtime's session (native Claude) as the
        `external` variant. `cwd` is the directory the turn ran in, as the hook or
        the transcript reported it. `native_session_id` doubles as the
        conversation's resumable handle (`external_id`).

        A turn already recorded by the driven runtime keeps its original run and
        messages. Its runtime identity lets native ingest skip the replay without
        copying it into the native ledger or saving transcript coordinates on it.

        The byte range is what marks a turn finished. A run carrying `end_offset` was
        assembled from the transcript and is final, so recording it again — a recovery
        overlapping a live tail, or a resume re-covering committed bytes — is a no-op
        returning `None` rather than a primary-key collision. That guard belongs here,
        at the durable sink: a caller's in-memory guard only knows the runs *it* wrote,
        never one another tailer already committed.

        A run with no byte range is provisional — the hooks' sketch of a turn still in
        flight, which sees the prompt and the answer but nothing of the thinking and
        tool calls between them. It exists so a live turn is a whole
        conversation → run → messages chain rather than messages with nowhere to hang.
        Recording over it replaces its contents in place, the transcript's full
        timeline superseding the sketch once the turn closes: the run keeps its row,
        and with it every history that includes it.
        """
        if not messages:
            return None
        driven = await self.driven_run(
            conversation.agent_tentacle_id, native_session_id, run_id
        )
        if driven is not None:
            return None
        run = ExternalAgentRun(
            id=run_id,
            conversation_id=conversation.id,
            native_id=conversation.agent_tentacle_id,
            native_turn_id=run_id,
            name=name,
            model_name=model_name,
            permission_mode=permission_mode,
            cwd=cwd,
            parent_run_id=parent_run_id,
            parent_tool_call_id=parent_tool_call_id,
            started_at=messages[0].timestamp,
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
                        noload(Conversation["messages"]),
                    ],
                )
                if owner is not None:
                    owner.external_id = native_session_id
                await session.commit()
                return run
        return await self.persist_run(
            run, conversation_id=conversation.id, external_id=native_session_id
        )

    async def persist_run(
        self,
        run: RunT,
        *,
        conversation_id: UUID7,
        external_id: str | None,
    ) -> RunT:
        """Persist a freshly built run, in the history of the conversation it ran
        in. `external_id`, when given, updates the resumable agent-session handle in
        the same commit.

        Only the new run and its messages are added, never the caller's whole
        conversation graph: re-merging it (prior runs and their message
        collections) can make SQLAlchemy believe a message was dropped from a
        stale run collection and null its NOT NULL `run_id`, raising an
        IntegrityError.
        """
        async with async_session() as session:
            session.add(run)
            reloaded = await session.one_or_none(
                Conversation,
                expressions=[Conversation["id"] == conversation_id],
                options=[
                    noload(Conversation["runs"]),
                    noload(Conversation["messages"]),
                ],
            )
            if reloaded is not None:
                reloaded.runs.append(run)
                reloaded.external_id = external_id
            await session.commit()
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
                    noload(Conversation["messages"]),
                ],
            )
            if stored_target is None:
                raise ValueError(f"unknown conversation {target.id}")
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
                    noload(Conversation["messages"]),
                    selectinload(Conversation["runs"]).noload(AgentRun["messages"]),
                ],
            )
            if origin is None:
                raise ValueError(f"unknown conversation {source.id}")
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
                    ["conversation_id", "run_id", "through_message_id"],
                    select(
                        literal(target.id),
                        ConversationRun["run_id"],
                        ConversationRun["through_message_id"],
                    ).where(
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
            current = await self.get(conversation.id, with_history=False)
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
                stored = await session.get(Conversation, conversation.id)
                if stored is None:
                    raise ValueError(f"unknown conversation {conversation.id}")
                session.add(transcript)
                await session.flush()
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

    async def drop_trailing_deferral(
        self,
        conversation: Conversation,
    ) -> ModelResponse | None:
        """If the conversation's last message is an abandoned deferred-tool
        ModelResponse (a tool-call request a new user turn supersedes), end the
        conversation's history just before it, remove it from the caller's copy,
        and return it. Without the bound the orphan would resurface mid-history on
        a cold reload, where it can no longer be recognized as a trailing deferral.

        The message itself stays: a fork may share its run and resume the call.
        """
        messages = conversation.messages
        if not messages:
            return None
        last = messages[-1]
        if not isinstance(last, ModelResponse):
            return None
        if not any(isinstance(part, ToolCallPart) for part in last.parts):
            return None
        previous = next(
            (
                message
                for message in reversed(messages)
                if message is not last and message.run_id == last.run_id
            ),
            None,
        )
        async with async_session() as session:
            entry = await session.one_or_none(
                ConversationRun,
                expressions=[
                    ConversationRun["conversation_id"] == conversation.id,
                    ConversationRun["run_id"] == last.run_id,
                ],
            )
            if entry is None:
                raise ValueError(
                    f"run {last.run_id} is not in conversation {conversation.id}"
                )
            if previous is None:
                # Nothing of the run comes before the call, so the run leaves this
                # history whole.
                await session.delete(entry)
            else:
                entry.through_message_id = previous.id
            await session.commit()
        conversation.messages.remove(last)
        return last

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
