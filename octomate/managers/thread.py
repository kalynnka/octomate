from __future__ import annotations

import uuid
from collections import OrderedDict
from datetime import UTC, datetime

from arcanus.materia.sqlalchemy import noload, selectinload
from sqlalchemy import and_, or_

from octomate.config.agents import AgentRouteModelName
from octomate.database import async_session
from octomate.managers.user import UserManager
from octomate.schemas.conversation import ChannelAddress
from octomate.schemas.events import MessageEvent
from octomate.schemas.messages import ModelRequest, ModelResponse
from octomate.schemas.project import Project
from octomate.schemas.segments import MarkdownSegment, MessageSegment, TextSegment
from octomate.schemas.thread import (
    ATTRIBUTABLE_KINDS,
    ChannelActorKind,
    Handoff,
    MessageBinding,
    MessageBindingKind,
    Thread,
    ThreadKey,
    ThreadMessage,
)
from octomate.schemas.user import UserProfile


def message_text_from_segments(segments: list[MessageSegment]) -> str | None:
    parts = [
        segment.data["text"]
        for segment in segments
        if isinstance(segment, (TextSegment, MarkdownSegment))
    ]
    if not parts:
        return None
    return "\n".join(parts)


class BindRefusal(ValueError):
    """A bind refused by policy — a thread that is not work, or one already bound —
    carrying the sentence a model may be told and correct from. A `ValueError`
    still, so nothing that never told the two apart changes."""


# What a listing can show of an opening line before it stops reading as a name.
TITLE_MAX = 72


def thread_title(text: str | None) -> str | None:
    """A name for a thread, out of one line of what was said in it.

    Whitespace is folded because a directive arrives with its own newlines and a
    listing has one line to give it. A message with nothing in it names nothing,
    and the row goes on falling back to its surface.
    """
    line = " ".join((text or "").split())
    if not line:
        return None
    return line if len(line) <= TITLE_MAX else f"{line[:TITLE_MAX].rstrip()}…"


class ThreadManager:
    """Owns durable thread chat ledger persistence.

    The cache mirrors `ConversationManager`: the database is the source of truth,
    and write methods keep the cached `Thread` coherent after commits.
    """

    def __init__(self, *, users: UserManager, cache_size: int = 256) -> None:
        # Every ledger row references its sender's registry profile — the host
        # constructs this manager around its one identity registry.
        self.users = users
        self.cache_size = cache_size
        self.threads: OrderedDict[ThreadKey, Thread] = OrderedDict()

    async def ensure(
        self,
        address_or_key: ChannelAddress | ThreadKey,
        *,
        project: Project | None = None,
    ) -> Thread:
        """The thread this key names, created if it is new.

        `project` attributes a thread being created and is ignored for one that
        already exists: a thread's project is frozen once the row is written, so
        declaring a project later attributes new threads rather than rewriting old
        ones. It is not part of the key either — the key is what the thread's history
        is filed under, and attribution must never move it.

        Only a thread and a native thread can take one; naming a project for a DM or a
        group chat raises rather than dropping it silently.
        """
        if isinstance(address_or_key, ChannelAddress):
            key = ThreadKey.from_address(address_or_key)
        else:
            key = address_or_key
        if project is not None and key.kind not in ATTRIBUTABLE_KINDS:
            raise ValueError(
                f"{key} is a {key.kind} and cannot be attributed to project "
                f"{project.name!r}: only a thread or a native_thread is work."
            )
        cached = self.threads.get(key)
        if cached is not None:
            self.threads.move_to_end(key)
            return cached

        async with async_session() as session:
            thread = await session.one_or_none(
                Thread,
                expressions=[
                    Thread["channel_tentacle_id"] == key.channel_tentacle_id,
                    Thread["chat_type"] == key.chat_type,
                    Thread["chat_id"] == key.chat_id,
                    Thread["channel_thread_id"] == key.channel_thread_id,
                ],
            )
            if thread is None:
                thread = Thread(
                    channel_tentacle_id=key.channel_tentacle_id,
                    chat_type=key.chat_type,
                    chat_id=key.chat_id,
                    channel_thread_id=key.channel_thread_id,
                    kind=key.kind,
                    project_id=project.id if project is not None else None,
                )
                session.add(thread)
            await session.flush()
            await thread.messages
            await thread.handoffs
            await thread.project
            await session.commit()

        self.cache_thread(thread)
        return thread

    async def rename(self, thread: Thread, title: str) -> Thread:
        """Give the thread the name the runtime running it grabbed for itself.

        Unlike the opening line `store_message` falls back to, this is a name for
        the work rather than for how it was asked for, so it overwrites — and goes
        on overwriting, because the runtime revises it as the session goes on. A
        name with nothing in it is not one, and leaves the thread as it was.
        """
        named = thread_title(title)
        if named is None or thread.title == named:
            return thread
        async with async_session() as session:
            stored = await session.get(Thread, thread.id)
            if stored is None:
                raise ValueError(f"unknown thread {thread.id}")
            stored.title = named
            await session.commit()
        thread.title = named
        self.cache_thread(thread)
        return thread

    async def bind(self, thread_id: uuid.UUID, project: Project) -> Thread:
        """Attribute a thread that is in no project to `project`, once.

        The one exception to a project being written when the row is created: a
        chat thread exists before anyone has said what it is about, so the
        attribution has to be able to arrive later. It arrives once. A second bind
        is refused rather than switching — the first project's workspace holds work
        nobody has reviewed yet, and a thread whose project moved is a record of
        what it did somewhere it no longer says it was. A different project is a
        different thread.

        A DM and a group chat are refused for the reason `ATTRIBUTABLE_KINDS`
        gives: they outlive every project in them. Both refusals are `BindRefusal`;
        a thread that does not exist is a plain `ValueError`, a wiring bug.

        No workspace is made here. A run's working directory is fixed when its
        process spawns, so this takes effect on the thread's next turn and the
        caller has to say so.
        """
        async with async_session() as session:
            thread = await session.get(Thread, thread_id)
            if thread is None:
                raise ValueError(f"no thread {thread_id} to bind")
            if thread.kind not in ATTRIBUTABLE_KINDS:
                raise BindRefusal(
                    f"{thread.key} is a {thread.kind} and cannot be attributed to "
                    f"project {project.name!r}: only a thread or a native_thread "
                    f"is work."
                )
            current = await thread.project
            if current is not None:
                raise BindRefusal(
                    f"{thread.key} is already about {current.name!r} and a thread "
                    f"binds once; work on {project.name!r} in its own thread."
                )
            thread.project_id = project.id
            await session.commit()

        # Read the thread back rather than returning the row that changed. `project`
        # is eagerly loaded, so the copy that set the id is still holding the answer
        # it was loaded with — and `ensure` hands the cached copy to every later turn
        # in the thread, which is exactly the run this binding is for.
        bound = await self.get(thread_id)
        if bound is None:
            raise ValueError(f"thread {thread_id} vanished while binding")
        return bound

    async def get(
        self, thread_id: uuid.UUID, *, with_messages: bool = True
    ) -> Thread | None:
        """The thread by primary key, or None. messages/handoffs are
        lazy="selectin", so the get loads them with the row.

        `with_messages=False` is for a reader that wants the row and not its
        ledger, and suppresses the load rather than dropping it afterwards —
        `noload` is the difference between not fetching a thread's messages and
        fetching them to throw away. It skips the cache with them, because a
        cached thread whose `messages` is empty is indistinguishable from one
        nobody has ever spoken in, and `ensure` hands that copy to channels.

        Either way the model ledger stays behind: it hangs off a message, and
        `related_model_messages` is how a caller asks for it — dragging it here
        would put a query per message behind every thread read.
        """
        options = (
            [selectinload(Thread["messages"]).noload(ThreadMessage["model_messages"])]
            if with_messages
            else [noload(Thread["messages"])]
        )
        async with async_session() as session:
            thread = await session.get(Thread, thread_id, options=options)
            if thread is None:
                return None

        if with_messages:
            self.cache_thread(thread)
        return thread

    async def list_threads(
        self,
        channel_tentacle_id: str | None = None,
        *,
        limit: int = 100,
    ) -> list[Thread]:
        """Threads most recently touched first — one channel's, or every
        channel's when `channel_tentacle_id` is None.

        Handoffs come with the rows (lazy="selectin", one batched pass); the
        ledgers do not. A listing that loaded them would read every message of
        every thread it names, and no caller has ever wanted that — the one
        reader of a thread's messages asks for that thread.
        """
        expressions = (
            []
            if channel_tentacle_id is None
            else [Thread["channel_tentacle_id"] == channel_tentacle_id]
        )
        async with async_session() as session:
            rows = await session.list(
                Thread,
                limit=limit,
                order_bys=[Thread["updated_at"].desc(), Thread["id"].desc()],
                options=[noload(Thread["messages"])],
                expressions=expressions,
            )
        return list(rows)

    def cache_thread(self, thread: Thread) -> None:
        self.threads[thread.key] = thread
        self.threads.move_to_end(thread.key)
        while len(self.threads) > self.cache_size:
            self.threads.popitem(last=False)

    async def store_message(self, message: ThreadMessage, thread: Thread) -> None:
        """Persist a ledger row and re-sync the cached thread from the database.

        A thread nobody has named takes its name from this row when a person wrote
        it. The listing carries no messages, so the first thing said is the only
        name a row has until a runtime grabs one of its own (`rename`).
        """
        async with async_session() as session:
            session.add(message)
            if message.direction == "inbound" and message.actor_kind == "human":
                row = await session.get(Thread, thread.id)
                if row is not None and row.title is None:
                    row.title = thread_title(message.message_text)
            await session.commit()
            reloaded = await session.get(Thread, thread.id)
            if reloaded is not None:
                await reloaded.messages
                await reloaded.handoffs
                self.cache_thread(reloaded)

    async def record_inbound(
        self,
        event: MessageEvent,
        *,
        actor_kind: ChannelActorKind = "human",
        agent_tentacle_id: str | None = None,
        happened_at: datetime | None = None,
    ) -> ThreadMessage:
        """Write the event to the thread's ledger.

        Swaps `event.sender` for its registry row as a side effect, so the
        event's sender line resolves the owning identity via `event.sender.user`.

        Pass `happened_at` only to overrule the event's own clock: a transcript replay
        knows when the turn really happened, which a hook-built event cannot carry.
        Left out, the event's clock is used, and failing that the moment it arrived —
        the row is never undated, because an undated row has no place in the order.
        """
        thread = await self.ensure(
            ThreadKey(
                channel_tentacle_id=event.tentacle_id,
                chat_type=event.chat_type,
                chat_id=event.chat_id,
                channel_thread_id=event.channel_thread_id,
            )
        )
        if happened_at is None and event.timestamp > 0:
            happened_at = datetime.fromtimestamp(event.timestamp, UTC)
        sender = await self.users.ensure_profile(
            thread.channel_tentacle_id, event.sender
        )
        # Swap in the registry row (owner eagerly loaded) so the event's sender
        # line resolves `user:{username}` through `event.sender.user`.
        event.sender = sender
        message = ThreadMessage(
            thread_id=thread.id,
            platform_message_id=event.message_id or None,
            reply_id=event.reply_id,
            happened_at=happened_at or datetime.now(UTC),
            direction="inbound",
            actor_kind=actor_kind,
            user_id=event.user_id,
            agent_tentacle_id=agent_tentacle_id,
            sender_id=sender.id,
            segments=event.segments,
            message_text=message_text_from_segments(event.segments),
            raw=event.raw,
        )
        await self.store_message(message, thread)
        return message

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
        """Write the agent's reply to the thread's ledger.
        Pass `happened_at` only when something knows better than this moment — a
        transcript replay does; an agent answering now does not.
        """
        if isinstance(thread_or_address, Thread):
            thread = thread_or_address
        else:
            thread = await self.ensure(thread_or_address)
        sender = await self.users.ensure_profile(thread.channel_tentacle_id, sender)
        message = ThreadMessage(
            thread_id=thread.id,
            platform_message_id=platform_message_id,
            reply_id=reply_id,
            happened_at=happened_at or datetime.now(UTC),
            direction="outbound",
            actor_kind=actor_kind,
            user_id="",
            agent_tentacle_id=agent_tentacle_id,
            sender_id=sender.id,
            segments=segments,
            message_text=message_text or message_text_from_segments(segments),
            raw=raw,
        )
        await self.store_message(message, thread)
        return message

    async def mark_presented(
        self,
        message: ThreadMessage,
        platform_message_id: str | None,
    ) -> ThreadMessage:
        if platform_message_id is None:
            return message
        message.platform_message_id = platform_message_id
        async with async_session() as session:
            stored = await session.get(ThreadMessage, message.id)
            if stored is None:
                raise ValueError(f"thread message {message.id} does not exist")
            stored.platform_message_id = platform_message_id
            await session.commit()
        return message

    async def redate_message(
        self, message: ThreadMessage, happened_at: datetime
    ) -> ThreadMessage:
        """Overrule a ledger row's clock after the fact: a transcript replay knows
        when a message really happened, while a row the live hooks wrote was stamped
        at receipt — a beat later than the transcript line, which is enough to sort a
        run's work above the prompt that caused it."""
        if message.happened_at == happened_at:
            return message
        message.happened_at = happened_at
        async with async_session() as session:
            stored = await session.get(ThreadMessage, message.id)
            if stored is None:
                raise ValueError(f"thread message {message.id} does not exist")
            stored.happened_at = happened_at
            await session.commit()
        return message

    async def pending_prompt_messages(
        self,
        thread: Thread,
        trigger_message_id: uuid.UUID,
        active_agent_id: str,
    ) -> list[ThreadMessage]:
        cached = await self.ensure(thread.key)

        async with async_session() as session:
            expressions = [
                ThreadMessage["thread_id"] == cached.id,
                ThreadMessage["id"] <= trigger_message_id,
                or_(
                    ThreadMessage["actor_kind"] != "agent",
                    ThreadMessage["agent_tentacle_id"] != active_agent_id,
                    ThreadMessage["agent_tentacle_id"].is_(None),
                ),
            ]
            if cached.source_cursor_message_id is not None:
                expressions.append(
                    ThreadMessage["id"] > cached.source_cursor_message_id
                )
            rows = await session.list(
                ThreadMessage,
                limit=None,
                order_bys=[ThreadMessage["id"]],
                expressions=expressions,
            )
        return list(rows)

    async def advance_prompt_cursor(
        self,
        thread: Thread,
        message_id: uuid.UUID,
    ) -> Thread | None:
        async with async_session() as session:
            stored = await session.get(Thread, thread.id)
            if stored is None:
                return None
            stored.source_cursor_message_id = message_id
            await session.commit()
        thread.source_cursor_message_id = message_id
        self.cache_thread(thread)
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
        async with async_session() as session:
            session.add(handoff)
            await session.commit()
        thread.handoffs.append(handoff)
        self.cache_thread(thread)
        return handoff

    async def bind_messages(
        self,
        thread_message_ids: list[uuid.UUID],
        model_message_id: uuid.UUID,
        *,
        kind: MessageBindingKind,
        run_id: str,
        tool_call_id: str | None = None,
    ) -> list[MessageBinding]:
        bindings: list[MessageBinding] = []
        async with async_session() as session:
            for position, thread_message_id in enumerate(thread_message_ids):
                binding = MessageBinding(
                    thread_message_id=thread_message_id,
                    model_message_id=model_message_id,
                    kind=kind,
                    run_id=run_id,
                    tool_call_id=tool_call_id,
                    position=position,
                )
                session.add(binding)
                bindings.append(binding)
            await session.commit()
        return bindings

    async def bind_assistant_replies(
        self,
        thread_message_ids: list[uuid.UUID],
        *,
        run_id: str,
    ) -> list[MessageBinding]:
        if not thread_message_ids:
            return []
        async with async_session() as session:
            responses = await session.list(
                ModelResponse,
                limit=None,
                order_bys=[ModelResponse["id"]],
                expressions=[
                    ModelResponse["run_id"] == run_id,
                    ModelResponse["role"] == "assistant",
                    ModelResponse["message_text"].is_not(None),
                    ModelResponse["message_text"] != "",
                ],
            )
        assistant_replies = list(responses)
        if not assistant_replies:
            return []
        if len(assistant_replies) == len(thread_message_ids):
            bindings: list[MessageBinding] = []
            async with async_session() as session:
                for thread_message_id, response in zip(
                    thread_message_ids, assistant_replies, strict=True
                ):
                    binding = MessageBinding(
                        thread_message_id=thread_message_id,
                        model_message_id=response.id,
                        kind="assistant_reply",
                        run_id=run_id,
                        position=0,
                    )
                    session.add(binding)
                    bindings.append(binding)
                await session.commit()
            return bindings
        return await self.bind_messages(
            thread_message_ids,
            assistant_replies[-1].id,
            kind="assistant_reply",
            run_id=run_id,
        )

    async def search_chat_messages(
        self,
        thread_id: uuid.UUID,
        query: str,
        *,
        actor_kind: ChannelActorKind | None = None,
        limit: int = 10,
    ) -> list[ThreadMessage]:
        expressions = [
            ThreadMessage["thread_id"] == thread_id,
            ThreadMessage["message_text"].ilike(f"%{query}%"),
        ]
        if actor_kind is not None:
            expressions.append(ThreadMessage["actor_kind"] == actor_kind)
        async with async_session() as session:
            rows = await session.list(
                ThreadMessage,
                limit=limit,
                order_bys=[ThreadMessage["happened_at"], ThreadMessage["id"]],
                expressions=expressions,
            )
        return list(rows)

    async def chat_messages_before(
        self,
        thread_id: uuid.UUID,
        anchor_id: uuid.UUID,
        *,
        limit: int = 5,
    ) -> list[ThreadMessage]:
        """The rows standing before `anchor_id` in the thread, oldest last of them
        first. Neighbours in the conversation, which is what the ledger's order means —
        so the anchor is compared on the same key the rows are sorted by."""
        async with async_session() as session:
            anchor = await session.get(ThreadMessage, anchor_id)
            if anchor is None:
                raise ValueError(f"thread message {anchor_id} does not exist")
            rows = await session.list(
                ThreadMessage,
                limit=limit,
                order_bys=[
                    ThreadMessage["happened_at"].desc(),
                    ThreadMessage["id"].desc(),
                ],
                expressions=[
                    ThreadMessage["thread_id"] == thread_id,
                    or_(
                        ThreadMessage["happened_at"] < anchor.happened_at,
                        and_(
                            ThreadMessage["happened_at"] == anchor.happened_at,
                            ThreadMessage["id"] < anchor.id,
                        ),
                    ),
                ],
            )
        return list(reversed(rows))

    async def chat_messages_after(
        self,
        thread_id: uuid.UUID,
        anchor_id: uuid.UUID,
        *,
        limit: int = 5,
    ) -> list[ThreadMessage]:
        """The rows standing after `anchor_id` in the thread, oldest first — the mirror
        of `chat_messages_before`, and anchored on the same key."""
        async with async_session() as session:
            anchor = await session.get(ThreadMessage, anchor_id)
            if anchor is None:
                raise ValueError(f"thread message {anchor_id} does not exist")
            rows = await session.list(
                ThreadMessage,
                limit=limit,
                order_bys=[ThreadMessage["happened_at"], ThreadMessage["id"]],
                expressions=[
                    ThreadMessage["thread_id"] == thread_id,
                    or_(
                        ThreadMessage["happened_at"] > anchor.happened_at,
                        and_(
                            ThreadMessage["happened_at"] == anchor.happened_at,
                            ThreadMessage["id"] > anchor.id,
                        ),
                    ),
                ],
            )
        return list(rows)

    async def related_model_messages(
        self,
        thread_message_id: uuid.UUID,
    ) -> list[ModelRequest | ModelResponse]:
        async with async_session() as session:
            message = await session.get(
                ThreadMessage,
                thread_message_id,
                options=[selectinload(ThreadMessage["model_messages"])],
            )
            if message is None:
                return []
            await message.model_messages
        return list(message.model_messages)
