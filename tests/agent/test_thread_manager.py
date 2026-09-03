from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic_ai.messages import ModelRequest as RawModelRequest
from pydantic_ai.messages import ModelResponse as RawModelResponse
from pydantic_ai.messages import TextPart, UserPromptPart
from sqlalchemy.ext.asyncio import AsyncEngine

from octomate.config.users import UserConfig
from octomate.database import async_session
from octomate.managers import ConversationManager, ThreadManager, UserManager
from octomate.schemas.conversation import ChannelAddress
from octomate.schemas.events import MessageEvent
from octomate.schemas.segments import TextSegment
from octomate.schemas.thread import MessageBinding, ThreadKey, ThreadMessage
from octomate.schemas.user import UserProfile


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

    fresh_thread = await ThreadManager(users=UserManager()).ensure(address("charlie"))

    assert alice_thread.id == bob_thread.id == fresh_thread.id
    assert [message.user_id for message in fresh_thread.messages] == ["alice", "bob"]


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
        from_agent_tentacle_id="inkling",
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

    hits = await manager.search_chat_messages(thread.id, "alpha")
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

    thread = await manager.ensure(ThreadKey.from_address(address()))
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

    thread = await manager.ensure(ThreadKey.from_address(address()))
    assert [m.message_text for m in thread.messages] == ["first", "second"]
    assert first.id < second.id


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
