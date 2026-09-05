from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import pytest
from arcanus.materia.sqlalchemy import selectinload
from pydantic_ai.messages import ModelRequest as RawModelRequest
from pydantic_ai.messages import ModelResponse as RawModelResponse
from pydantic_ai.messages import TextPart, UserPromptPart
from sqlalchemy import event as sqlalchemy_event
from sqlalchemy.ext.asyncio import AsyncEngine

from octomate.config.users import UserConfig
from octomate.database import async_session
from octomate.managers import ConversationManager, ThreadManager, UserManager
from octomate.schemas.conversation import ChannelAddress
from octomate.schemas.events import MessageEvent
from octomate.schemas.segments import TextSegment
from octomate.schemas.thread import (
    ATTRIBUTABLE_KINDS,
    MessageBinding,
    Thread,
    ThreadKey,
    ThreadMessage,
)
from octomate.schemas.user import UserProfile
from tests.support.managers import a_loaded_thread


@pytest.fixture(autouse=True)
async def _db(in_memory_engine: AsyncEngine) -> None:
    return


def address(user_id: str = "alice") -> ChannelAddress:
    return ChannelAddress(
        channel_tentacle_id="slack",
        chat_type="thread",
        chat_id="C123",
        user_id=user_id,
        channel_thread_id="1710000000.000001",
    )


def event(message_id: str, user_id: str, text: str) -> MessageEvent:
    return MessageEvent(
        tentacle_id="slack",
        message_id=message_id,
        channel_thread_id="1710000000.000001",
        timestamp=1710000000.0,
        user_id=user_id,
        chat_id="C123",
        chat_type="thread",
        sender=UserProfile(channel_user_id=user_id, name=user_id.title()),
        segments=[TextSegment(data={"text": text})],
        raw=text,
    )


async def test_ensure_thread_ignores_sender_user_id() -> None:
    manager = ThreadManager(users=UserManager())

    alice_thread = await manager.ensure(address("alice"))
    bob_thread = await manager.ensure(address("bob"))
    await manager.record_inbound(event("m1", "alice", "alpha"))
    await manager.record_inbound(event("m2", "bob", "beta"))

    fresh_thread = await a_loaded_thread(
        ThreadManager(users=UserManager()), address("charlie")
    )

    assert alice_thread.id == bob_thread.id == fresh_thread.id
    assert [message.user_id for message in fresh_thread.messages] == ["alice", "bob"]


async def test_concurrent_first_sightings_of_one_key_insert_once() -> None:
    """The bug this manager was missing a guard for: two coroutines that miss
    together both insert, and the loser trips the threads UNIQUE constraint —
    which escapes into `follow`'s handler and strands the rest of the session.
    A session's follow task preparing while a hook pokes it does exactly this,
    and so do two people replying at once in a chat with no thread row yet."""
    manager = ThreadManager(users=UserManager())
    key = ThreadKey(
        channel_tentacle_id="slack",
        chat_type="thread",
        chat_id="C123",
        channel_thread_id="1.5",
    )

    threads = await asyncio.gather(*(manager.ensure(key) for _ in range(4)))

    assert len({thread.id for thread in threads}) == 1
    async with async_session() as session:
        assert await session.count(Thread) == 1


async def test_a_re_delivered_message_is_recorded_once() -> None:
    """A platform re-sends when it misses an ack, and the second send is the same
    message. Recording is idempotent: asking again is answered with the row that
    already exists, not with a second one."""
    manager = ThreadManager(users=UserManager())
    thread = await manager.ensure(address())
    first = await manager.record_inbound(event("m1", "alice", "hello"))

    again = await manager.record_inbound(event("m1", "alice", "hello"))

    assert again.id == first.id
    loaded = await manager.get(thread.id)
    assert loaded is not None
    assert [message.id for message in loaded.messages] == [first.id]


async def test_a_message_the_platform_never_named_is_always_recorded() -> None:
    """There is nothing to recognise it by, so refusing one would drop real
    messages rather than duplicates."""
    manager = ThreadManager(users=UserManager())
    thread = await manager.ensure(address())

    first = await manager.record_inbound(event("", "alice", "hello"))
    second = await manager.record_inbound(event("", "alice", "hello"))

    assert first.id != second.id
    loaded = await manager.get(thread.id)
    assert loaded is not None
    assert len(loaded.messages) == 2


async def test_a_reply_may_share_the_id_of_the_message_it_answers() -> None:
    """A native runtime files a turn's prompt and its answer under one id — the
    pair is what its transcript names — so direction is part of the key."""
    manager = ThreadManager(users=UserManager())
    thread = await manager.ensure(address())

    inbound = await manager.record_inbound(event("m1", "alice", "asked"))
    outbound = await manager.record_outbound(
        thread,
        agent_tentacle_id="inkling",
        segments=[TextSegment(data={"text": "answered"})],
        sender=UserProfile(channel_user_id="bot", name="Bot"),
        platform_message_id="m1",
    )

    assert outbound.platform_message_id == inbound.platform_message_id


async def test_pending_prompt_messages_and_cursor_skip_active_agent_output() -> None:
    manager = ThreadManager(users=UserManager())
    thread = await manager.ensure(address())
    human_message = await manager.record_inbound(event("m1", "alice", "human asks"))
    bot_message = await manager.record_inbound(
        event("m2", "bot", "bot adds detail"),
        actor_kind="bot",
    )
    own_output = await manager.record_outbound(
        thread,
        agent_tentacle_id="inkling",
        segments=[TextSegment(data={"text": "inkling already answered"})],
        sender=UserProfile(channel_user_id="bot", name="Bot"),
    )
    other_agent_output = await manager.record_outbound(
        thread,
        agent_tentacle_id="claude",
        segments=[TextSegment(data={"text": "claude added context"})],
        sender=UserProfile(channel_user_id="bot", name="Bot"),
    )
    trigger = await manager.record_inbound(event("m3", "alice", "wake now"))

    pending = await manager.pending_prompt_messages(
        thread,
        trigger.id,
        active_agent_id="inkling",
    )
    assert [message.id for message in pending] == [
        human_message.id,
        bot_message.id,
        other_agent_output.id,
        trigger.id,
    ]
    assert own_output.id not in {message.id for message in pending}

    stored = await manager.advance_prompt_cursor(thread, bot_message.id)
    hot = await manager.ensure(address())
    assert stored is not None
    assert hot.source_cursor_message_id == bot_message.id

    pending_after_cursor = await manager.pending_prompt_messages(
        thread,
        trigger.id,
        active_agent_id="inkling",
    )
    assert [message.id for message in pending_after_cursor] == [
        other_agent_output.id,
        trigger.id,
    ]


async def test_pending_prompt_messages_ensures_thread() -> None:
    manager = ThreadManager(users=UserManager())
    thread = await manager.ensure(address())
    trigger = await manager.record_inbound(event("m1", "alice", "wake now"))

    pending = await ThreadManager(users=UserManager()).pending_prompt_messages(
        thread,
        trigger.id,
        active_agent_id="inkling",
    )

    assert [message.id for message in pending] == [trigger.id]


async def test_record_inbound_stamps_linked_identity_on_the_event() -> None:
    users = UserManager(
        {
            "luhui": UserConfig.model_validate(
                {
                    "name": "Lu Hui",
                    "profiles": {
                        "slack": {"channel_user_id": "U1"},
                        "lark": {"channel_user_id": "ou_1"},
                    },
                }
            )
        }
    )
    await users.reconcile()
    manager = ThreadManager(users=users)

    slack = event("m1", "U1", "hello from slack")
    await manager.record_inbound(slack)
    lark = MessageEvent(
        tentacle_id="lark",
        message_id="m2",
        channel_thread_id="t1",
        timestamp=1710000000.0,
        user_id="ou_1",
        chat_id="oc_1",
        chat_type="thread",
        sender=UserProfile(channel_user_id="ou_1", name="陆晖"),
        segments=[TextSegment(data={"text": "hello from lark"})],
        raw="hello from lark",
    )
    await manager.record_inbound(lark)

    # One human, two channels, one stable marker in both prompts — resolved
    # through event.sender.user, not a stamped field.
    slack_owner = await slack.sender.user
    assert slack_owner is not None
    assert slack_owner.name == "Lu Hui"
    assert "(U1, user:luhui)" in str(slack)
    assert "(ou_1, user:luhui)" in str(lark)


async def test_sender_line_leaves_an_undeclared_sender_as_a_visitor() -> None:
    manager = ThreadManager(users=UserManager())

    stranger = event("m1", "alice", "plain")
    await manager.record_inbound(stranger)

    assert stranger.sender.user.peek() is None
    assert stranger.sender.user_id is None
    assert "(alice)" in str(stranger)
    assert "user:" not in str(stranger)


async def test_record_handoff_updates_the_active_owner() -> None:
    manager = ThreadManager(users=UserManager())
    thread = await manager.ensure(address())

    await manager.record_handoff(
        thread,
        to_agent_tentacle_id="inkling",
        to_model="haiku",
        reason="Initial owner.",
    )
    await manager.record_handoff(
        thread,
        source_agent_tentacle_id="inkling",
        to_agent_tentacle_id="claude",
        to_model="sonnet",
        reason="Needs code work.",
    )

    hot = await manager.ensure(address())

    assert hot.active_agent_tentacle_id == "claude"
    assert hot.active_model == "sonnet"
    assert [handoff.to_agent_tentacle_id for handoff in hot.handoffs] == [
        "inkling",
        "claude",
    ]


async def test_chat_history_search_paging_and_message_bindings() -> None:
    manager = ThreadManager(users=UserManager())
    thread = await manager.ensure(address())
    first = await manager.record_inbound(event("m1", "alice", "alpha first"))
    second = await manager.record_inbound(event("m2", "bob", "beta second"))
    third = await manager.record_inbound(event("m3", "alice", "alpha third"))
    alice = await manager.users.profile("slack", "alice")
    assert alice is not None

    hits = await manager.search_chat_messages(alice, "alpha")
    before = await manager.chat_messages_before(thread.id, third.id, limit=1)
    after = await manager.chat_messages_after(thread.id, first.id, limit=1)

    assert [message.id for message in hits] == [first.id, third.id]
    assert [message.id for message in before] == [second.id]
    assert [message.id for message in after] == [second.id]

    conversation_manager = ConversationManager()
    conversation = await conversation_manager.ensure(
        thread.id,
        agent_tentacle_id="inkling",
    )
    run_id = "run-chat-binding"
    await conversation_manager.record_agent_run(
        conversation,
        run_id=run_id,
        messages=[
            RawModelRequest(
                parts=[UserPromptPart(content="alpha first\nbeta second")],
                run_id=run_id,
                conversation_id=str(conversation.id),
                timestamp=datetime.now(UTC),
            )
        ],
    )
    model_message = (
        await conversation_manager.search_messages(conversation.id, "alpha")
    )[0]

    bindings = await manager.bind_messages(
        [first.id, second.id],
        model_message.id,
        kind="request_source",
        run_id=run_id,
    )

    async with async_session() as session:
        stored_message = await session.one_or_none(
            ThreadMessage,
            expressions=[ThreadMessage["id"] == first.id],
        )
        assert stored_message is not None
        await stored_message.model_messages

    assert [binding.position for binding in bindings] == [0, 1]
    assert stored_message.model_messages[0].id == model_message.id


async def test_assistant_reply_binding_uses_persisted_response() -> None:
    manager = ThreadManager(users=UserManager())
    thread = await manager.ensure(address())
    reply = await manager.record_outbound(
        thread,
        agent_tentacle_id="inkling",
        segments=[TextSegment(data={"text": "visible answer"})],
        platform_message_id="bot-reply-1",
        sender=UserProfile(channel_user_id="bot", name="Bot"),
    )

    conversation_manager = ConversationManager()
    conversation = await conversation_manager.ensure(
        thread.id,
        agent_tentacle_id="inkling",
    )
    run_id = "run-assistant-reply-binding"
    await conversation_manager.record_agent_run(
        conversation,
        run_id=run_id,
        messages=[
            RawModelRequest(
                parts=[UserPromptPart(content="question")],
                run_id=run_id,
                conversation_id=str(conversation.id),
                timestamp=datetime.now(UTC),
            ),
            RawModelResponse(
                parts=[TextPart(content="visible answer")],
                run_id=run_id,
                conversation_id=str(conversation.id),
                timestamp=datetime.now(UTC),
            ),
        ],
    )
    model_message = (
        await conversation_manager.search_messages(
            conversation.id, "visible answer", role="assistant"
        )
    )[0]

    bindings = await manager.bind_assistant_replies([reply.id], run_id=run_id)

    async with async_session() as session:
        stored_message = await session.one_or_none(
            ThreadMessage,
            expressions=[ThreadMessage["id"] == reply.id],
        )
        stored_binding = await session.one_or_none(
            MessageBinding,
            expressions=[
                MessageBinding["thread_message_id"] == reply.id,
                MessageBinding["model_message_id"] == model_message.id,
                MessageBinding["kind"] == "assistant_reply",
            ],
        )
        assert stored_message is not None
        assert stored_binding is not None
        await stored_message.model_messages

    assert [binding.thread_message_id for binding in bindings] == [reply.id]
    assert bindings[0].model_message_id == model_message.id
    assert stored_message.model_messages[0].id == model_message.id


async def test_history_written_late_still_sorts_where_it_happened() -> None:
    """The ledger's order is the conversation's, not the order Octomate learned it in.

    A session Octomate meets mid-conversation is exactly this shape: the live turn is
    written first, and the history behind it is backfilled afterwards — so the rows
    written last are the ones that happened first. The uuid7 `id` carries the moment of
    writing, so ordering on it puts a newcomer at the head of its own history.
    """
    manager = ThreadManager(users=UserManager())
    earlier = datetime(2026, 7, 9, 10, 0, tzinfo=UTC)
    later = datetime(2026, 7, 9, 11, 0, tzinfo=UTC)

    # The live turn lands first...
    live = await manager.record_inbound(
        event("m-live", "alice", "and now this"), happened_at=later
    )
    # ...and only then is the history that precedes it replayed off a transcript.
    replayed = await manager.record_inbound(
        event("m-old", "alice", "this came first"), happened_at=earlier
    )

    thread = await a_loaded_thread(manager, ThreadKey.from_address(address()))
    assert [m.message_text for m in thread.messages] == [
        "this came first",
        "and now this",
    ]
    # Written in the opposite order to the one it reads back in — the point of the test.
    assert replayed.id > live.id


async def test_one_instant_keeps_the_order_it_was_written_in() -> None:
    """`happened_at` alone is not a total order: a transcript line can produce several
    messages at one instant, and a clock has finite resolution. `id` breaks the tie, so
    rows sharing an instant stay in the order they were written rather than shuffling."""
    manager = ThreadManager(users=UserManager())
    same = datetime(2026, 7, 9, 10, 0, tzinfo=UTC)

    first = await manager.record_inbound(
        event("m-1", "alice", "first"), happened_at=same
    )
    second = await manager.record_inbound(
        event("m-2", "alice", "second"), happened_at=same
    )

    thread = await a_loaded_thread(manager, ThreadKey.from_address(address()))
    assert [m.message_text for m in thread.messages] == ["first", "second"]
    assert first.id < second.id


async def test_a_kick_never_reads_the_rooms_whole_ledger(
    in_memory_engine: AsyncEngine,
) -> None:
    """The bound this manager exists to keep: a room's ledger has no ceiling, so
    the reads a turn makes must not grow with its tenure.

    `ensure` is what every inbound event and every tailer commit goes through, and
    `find_message` is how the hooks ask whether they already wrote a turn's row.
    Neither may fetch a chat row it was not asked for — the regression is a
    `selectinload` creeping back onto either, which reads the whole room to answer
    a question about one row.
    """
    manager = ThreadManager(users=UserManager())
    for index in range(12):
        await manager.record_inbound(event(f"m-{index}", "alice", f"line {index}"))

    rows_read: list[str] = []

    @sqlalchemy_event.listens_for(in_memory_engine.sync_engine, "before_cursor_execute")
    def record(
        conn: object,
        cursor: object,
        statement: str,
        parameters: object,
        context: object,
        executemany: bool,
    ) -> None:
        if "FROM thread_messages" in statement:
            rows_read.append(statement)

    thread = await manager.ensure(address())
    assert rows_read == [], "ensure read the ledger it was not asked for"

    found = await manager.find_message(thread.id, "m-7", "inbound")
    assert found is not None
    assert found.message_text == "line 7"
    # One statement, and it carries the key rather than filtering in Python.
    assert len(rows_read) == 1, rows_read
    assert "platform_message_id" in rows_read[0]

    # And the reader that does want the whole thing still gets it.
    thread = await a_loaded_thread(manager, address())
    assert len(list(thread.messages)) == 12


async def test_a_row_is_never_undated() -> None:
    """The order is only total if every row carries a `happened_at` — and every outbound
    caller but the tailers passes none, so the manager owns the answer rather than the
    call sites."""
    manager = ThreadManager(users=UserManager())

    inbound = await manager.record_inbound(
        MessageEvent(
            tentacle_id="slack",
            chat_id="C123",
            chat_type="group",
            segments=[TextSegment(data={"text": "no clock on this event"})],
        )
    )
    outbound = await manager.record_outbound(
        ThreadKey.from_address(address()),
        agent_tentacle_id="a",
        segments=[TextSegment(data={"text": "hi"})],
        sender=UserProfile(channel_user_id="bot", name="Bot"),
    )

    assert inbound.happened_at is not None
    assert outbound.happened_at is not None


def said(
    tentacle_id: str, message_id: str, user_id: str, text: str, at: float
) -> MessageEvent:
    """A direct message from `user_id` on `tentacle_id`, in their own chat."""
    return MessageEvent(
        tentacle_id=tentacle_id,
        message_id=message_id,
        timestamp=at,
        user_id=user_id,
        chat_id=user_id,
        chat_type="dm",
        sender=UserProfile(channel_user_id=user_id, name=user_id.title()),
        segments=[TextSegment(data={"text": text})],
    )


async def test_a_persons_history_is_every_thread_their_accounts_spoke_in() -> None:
    """Registered, alice is one person on slack and on lark: read as either
    account, her history holds both threads. Bob's own direct messages, where she
    never spoke, are his and not hers."""
    users = UserManager(
        {
            "alice": UserConfig.model_validate(
                {
                    "secret": "alice-token",
                    "profiles": {
                        "slack": {"channel_user_id": "alice"},
                        "lark": {"channel_user_id": "ou_alice"},
                    },
                }
            )
        }
    )
    await users.reconcile()
    manager = ThreadManager(users=users)
    await manager.record_inbound(event("m1", "alice", "alpha on slack"))
    await manager.record_inbound(
        said("lark", "l1", "ou_alice", "alpha on lark", 1710000001.0)
    )
    await manager.record_inbound(
        said("slack", "b1", "bob", "alpha, bob alone", 1710000002.0)
    )
    alice = await users.profile("lark", "ou_alice")
    bob = await users.profile("slack", "bob")
    assert alice is not None
    assert bob is not None

    hers = await manager.search_chat_messages(alice, "alpha")
    his = await manager.search_chat_messages(bob, "alpha")

    assert [message.message_text for message in hers] == [
        "alpha on slack",
        "alpha on lark",
    ]
    assert [message.message_text for message in his] == ["alpha, bob alone"]


async def test_chat_message_resolves_a_handle_within_a_persons_history() -> None:
    manager = ThreadManager(users=UserManager())
    first = await manager.record_inbound(event("1710000000.000002", "alice", "first"))
    await manager.record_inbound(said("slack", "b1", "bob", "bob alone", 1710000001.0))
    alice = await manager.users.profile("slack", "alice")
    bob = await manager.users.profile("slack", "bob")
    assert alice is not None
    assert bob is not None

    assert (await manager.chat_message(alice, "#msg:1710000000.000002")).id == first.id
    assert (await manager.chat_message(alice, str(first.id))).id == first.id
    with pytest.raises(ValueError, match="no message #msg:nope"):
        await manager.chat_message(alice, "#msg:nope")
    # Bob never spoke in alice's thread: her message is not his to name.
    with pytest.raises(ValueError, match="no message"):
        await manager.chat_message(bob, str(first.id))


def chat_room() -> ThreadKey:
    return ThreadKey(channel_tentacle_id="slack", chat_type="dm", chat_id="D9")


async def test_a_chat_room_can_hold_more_than_one_sub_thread() -> None:
    """The key that pins one row per chat would otherwise refuse the second: a
    sub-thread shares its chat room's address, having none of its own."""
    manager = ThreadManager(users=UserManager())
    parent = await manager.ensure(chat_room())

    first = await manager.open_sub_thread(parent)
    second = await manager.open_sub_thread(parent)

    assert first.id != second.id
    assert first.parent_thread_id == parent.id
    assert second.parent_thread_id == parent.id
    # The chat room's own address is untouched, and still resolves to it.
    assert (await manager.ensure(chat_room())).id == parent.id


async def test_a_sub_thread_shares_its_chat_rooms_address() -> None:
    """Nothing is invented here. A channel sends a reply to whatever
    `channel_thread_id` holds, so a made-up one would be posted at the platform —
    and sharing the chat room's is what makes a sub-thread's key resolve to it."""
    manager = ThreadManager(users=UserManager())
    parent = await manager.ensure(chat_room())

    child = await manager.open_sub_thread(parent)

    assert child.channel_thread_id == parent.channel_thread_id
    assert (await manager.ensure(chat_room())).id == parent.id
    # Its kind is the chat room's, so it stays out of ATTRIBUTABLE_KINDS and
    # carries no project — work in a dm or a group chat is not attributable, and a
    # sub-thread does not change that.
    assert child.kind == "dm"
    assert child.kind not in ATTRIBUTABLE_KINDS


async def test_a_chat_room_is_one_row_however_its_address_is_spelled() -> None:
    """A channel reporting no thread writes `""` where another writes NULL. They
    name the same chat, and a key that kept both spellings would file it twice —
    under different unique indexes, so neither would catch the other."""
    manager = ThreadManager(users=UserManager())
    blank = ThreadKey(
        channel_tentacle_id="slack",
        chat_type="dm",
        chat_id="D9",
        channel_thread_id="",
    )

    assert blank.channel_thread_id is None
    assert blank == chat_room()
    assert (await manager.ensure(blank)).id == (await manager.ensure(chat_room())).id


async def test_entering_a_chat_room_opens_a_sub_thread() -> None:
    """What a kick in a dm or a group chat actually runs in."""
    manager = ThreadManager(users=UserManager())

    entered = await manager.enter(chat_room())

    assert entered.parent_thread_id == (await manager.ensure(chat_room())).id


async def test_entering_a_thread_is_the_thread_itself() -> None:
    """A thread is a piece of work already, so nothing is opened inside it and the
    turn runs where it always did."""
    manager = ThreadManager(users=UserManager())

    entered = await manager.enter(address())

    assert entered.parent_thread_id is None
    assert entered.id == (await manager.ensure(address())).id


async def test_each_kick_in_a_chat_room_enters_its_own_sub_thread() -> None:
    """The discontinuity this whole shape exists for: two kicks in one chat room
    answer in two threads, and so in two model contexts."""
    manager = ThreadManager(users=UserManager())

    first = await manager.enter(chat_room())
    second = await manager.enter(chat_room())

    assert first.id != second.id
    assert first.parent_thread_id == second.parent_thread_id


async def test_entering_the_chat_room_a_turn_is_in_keeps_its_sub_thread() -> None:
    """An agent taking the conversation over in place is still answering the same
    kick, and a second sub-thread would split one answer across two contexts."""
    manager = ThreadManager(users=UserManager())
    entered = await manager.enter(chat_room())

    again = await manager.enter(chat_room(), current=entered)

    assert again.id == entered.id


async def test_a_chat_room_nests_one_level() -> None:
    """The rule that keeps `surface` a single hop rather than a walk."""
    manager = ThreadManager(users=UserManager())
    parent = await manager.ensure(chat_room())
    child = await manager.open_sub_thread(parent)

    with pytest.raises(ValueError, match="nests one level"):
        await manager.open_sub_thread(child)


async def test_a_sub_threads_surface_is_its_chat_room() -> None:
    """The split this whole shape rests on: the work is the sub-thread's, the
    surface is the chat room, and one call says which is which."""
    manager = ThreadManager(users=UserManager())
    parent = await manager.ensure(chat_room())
    child = await manager.open_sub_thread(parent)

    assert (await manager.surface(child)).id == parent.id
    # A thread nobody opened is its own surface, so the caller never branches.
    assert (await manager.surface(parent)).id == parent.id


async def test_a_listing_names_chat_rooms_and_not_their_sub_threads() -> None:
    """A listing names the surfaces a person can open. A chat room's sub-threads
    are reached through it, and a busy one would otherwise bury every other."""
    manager = ThreadManager(users=UserManager())
    parent = await manager.ensure(chat_room())
    await manager.open_sub_thread(parent)
    await manager.open_sub_thread(parent)
    work = await manager.ensure(address())

    listed = {thread.id for thread in await manager.list_threads()}

    assert listed == {parent.id, work.id}


async def test_the_parent_relation_loads_the_chat_room_when_asked() -> None:
    """`remote_side` points the self-reference at the parent rather than at the
    children, and `raise_on_sql` keeps it off every read that did not ask: a listing
    that fetched each row's parent would read the same chat rooms over again."""
    manager = ThreadManager(users=UserManager())
    parent = await manager.ensure(chat_room())
    child = await manager.open_sub_thread(parent)

    async with async_session() as session:
        rows = await session.list(
            Thread,
            limit=1,
            options=[selectinload(Thread["parent"])],
            expressions=[Thread["id"] == child.id],
        )
        loaded = rows[0].parent.peek()

    assert loaded is not None
    assert loaded.id == parent.id
