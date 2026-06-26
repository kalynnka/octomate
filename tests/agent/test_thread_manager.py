from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import datetime, timezone

import pytest
from pydantic_ai.messages import ModelRequest as RawModelRequest
from pydantic_ai.messages import UserPromptPart
from sqlalchemy.ext.asyncio import AsyncEngine

from octomate.database import async_session
from octomate.managers import ConversationManager, ThreadManager
from octomate.schemas.channel import ThreadMessage
from octomate.schemas.conversation import ChannelAddress, UserProfile
from octomate.schemas.events import MessageEvent
from octomate.schemas.segments import TextSegment


@pytest.fixture(autouse=True)
async def _db(in_memory_engine: AsyncEngine) -> AsyncIterator[None]:
    yield


def address(user_id: str = "alice") -> ChannelAddress:
    return ChannelAddress(
        channel_tentacle_id="slack",
        chat_type="group",
        chat_id="C123",
        user_id=user_id,
        thread_id="1710000000.000001",
    )


def event(message_id: str, user_id: str, text: str) -> MessageEvent:
    return MessageEvent(
        tentacle_id="slack",
        message_id=message_id,
        thread_id="1710000000.000001",
        timestamp=1710000000.0,
        user_id=user_id,
        chat_id="C123",
        chat_type="group",
        sender=UserProfile(user_id=user_id, name=user_id.title()),
        segments=[TextSegment(data={"text": text})],
        raw=text,
    )


async def test_ensure_thread_ignores_sender_user_id() -> None:
    manager = ThreadManager()

    alice_thread = await manager.ensure_thread(address("alice"))
    bob_thread = await manager.ensure_thread(address("bob"))
    await manager.record_inbound(event("m1", "alice", "alpha"))
    await manager.record_inbound(event("m2", "bob", "beta"))

    fresh_thread = await ThreadManager().ensure_thread(address("charlie"))

    assert alice_thread.id == bob_thread.id == fresh_thread.id
    assert [message.user_id for message in fresh_thread.messages] == ["alice", "bob"]


async def test_pending_prompt_messages_and_cursor_skip_active_agent_output() -> None:
    manager = ThreadManager()
    thread = await manager.ensure_thread(address())
    human_message = await manager.record_inbound(event("m1", "alice", "human asks"))
    bot_message = await manager.record_inbound(
        event("m2", "bot", "bot adds detail"),
        actor_kind="bot",
    )
    own_output = await manager.record_outbound(
        thread,
        agent_tentacle_id="inkling",
        segments=[TextSegment(data={"text": "inkling already answered"})],
    )
    other_agent_output = await manager.record_outbound(
        thread,
        agent_tentacle_id="claude",
        segments=[TextSegment(data={"text": "claude added context"})],
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
    hot = await manager.ensure_thread(address())
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
    manager = ThreadManager()
    thread = await manager.ensure_thread(address())
    trigger = await manager.record_inbound(event("m1", "alice", "wake now"))

    pending = await ThreadManager().pending_prompt_messages(
        thread,
        trigger.id,
        active_agent_id="inkling",
    )

    assert [message.id for message in pending] == [trigger.id]


async def test_record_handoff_syncs_active_owner_cache() -> None:
    manager = ThreadManager()
    thread = await manager.ensure_thread(address())

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

    hot = await manager.ensure_thread(address())

    assert hot.active_agent_tentacle_id == "claude"
    assert hot.active_model == "sonnet"
    assert [handoff.to_agent_tentacle_id for handoff in hot.handoffs] == [
        "inkling",
        "claude",
    ]


async def test_chat_history_search_paging_and_message_bindings() -> None:
    manager = ThreadManager()
    thread = await manager.ensure_thread(address())
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
        address(),
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
                timestamp=datetime.now(timezone.utc),
            )
        ],
    )
    model_message = (await conversation_manager.search_messages(conversation.id, "alpha"))[
        0
    ]

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
