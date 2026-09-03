from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from arcanus import Relation
from pydantic_ai.messages import ModelRequest as RawModelRequest
from pydantic_ai.messages import UserPromptPart
from sqlalchemy import inspect
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine

from octomate.database import async_session
from octomate.managers import ConversationManager
from octomate.models import Base
from octomate.models.messages import ModelMessage as ModelMessageModel
from octomate.models.thread import MessageBinding as MessageBindingModel
from octomate.models.thread import Thread as ThreadModel
from octomate.models.thread import ThreadMessage as ThreadMessageModel
from octomate.schemas.conversation import ChannelAddress
from octomate.schemas.segments import TextSegment
from octomate.schemas.thread import (
    Handoff,
    MessageBinding,
    Thread,
    ThreadKey,
    ThreadMessage,
)
from octomate.schemas.user import UserProfile


@pytest.fixture(autouse=True)
async def _db(in_memory_engine: AsyncEngine) -> None:
    return


def _address(*, user_id: str) -> ChannelAddress:
    return ChannelAddress(
        channel_tentacle_id="slack",
        chat_type="thread",
        chat_id="C123",
        user_id=user_id,
        channel_thread_id="1710000000.000001",
    )


def test_thread_tables_are_registered() -> None:
    assert "threads" in Base.metadata.tables
    assert "thread_messages" in Base.metadata.tables
    assert "channel_handoffs" in Base.metadata.tables
    assert "message_binding" in Base.metadata.tables
    assert "thread_id" in Base.metadata.tables["conversations"].columns


def test_message_relationships_use_message_binding() -> None:
    channel_relationship = inspect(ThreadMessageModel).relationships["model_messages"]
    model_relationship = inspect(ModelMessageModel).relationships["thread_messages"]

    assert channel_relationship.secondary is MessageBindingModel.__table__
    assert model_relationship.secondary is MessageBindingModel.__table__
    assert model_relationship.lazy == "raise_on_sql"


def test_a_thread_does_not_carry_its_conversations() -> None:
    """Nothing reads conversations off a thread — `Thread` has no such field, and
    a reader asks ConversationManager. Eager here would put each conversation,
    its runs, and their model messages behind every single thread read."""
    assert inspect(ThreadModel).relationships["conversations"].lazy == "noload"


def test_no_relationship_loads_on_attribute_access() -> None:
    """`select`, SQLAlchemy's default, turns an attribute read into a query: IO
    with no `await` marking it, which outside a greenlet fails and inside one is
    a query nobody wrote. Every relationship states something else instead —
    `selectin` to come with the row, `raise_on_sql` to make a reader ask, or
    `noload` for one that exists for a cascade. A caller overrides per query."""
    implicit = sorted(
        f"{mapper.class_.__name__}.{name}"
        for mapper in Base.registry.mappers
        for name, relationship in mapper.relationships.items()
        if relationship.lazy == "select"
    )
    assert implicit == []


def test_channel_handoff_sorts_by_uuid7_id() -> None:
    thread_id = uuid.UUID("00000000-0000-7000-8000-000000000100")
    earlier_handoff = Handoff(
        id=uuid.UUID("00000000-0000-7000-8000-000000000001"),
        thread_id=thread_id,
        to_agent_tentacle_id="inkling",
        created_at=datetime(2030, 1, 1, tzinfo=UTC),
    )
    later_handoff = Handoff(
        id=uuid.UUID("00000000-0000-7000-8000-000000000002"),
        thread_id=thread_id,
        to_agent_tentacle_id="claude",
        created_at=datetime(2020, 1, 1, tzinfo=UTC),
    )

    assert earlier_handoff < later_handoff
    assert later_handoff > earlier_handoff
    assert sorted([later_handoff, earlier_handoff]) == [
        earlier_handoff,
        later_handoff,
    ]


def test_thread_key_ignores_sender_user() -> None:
    assert ThreadKey.from_address(_address(user_id="alice")) == (
        ThreadKey.from_address(_address(user_id="bob"))
    )


async def test_thread_round_trips_with_messages_and_handoffs() -> None:
    thread = Thread(
        channel_tentacle_id="slack",
        chat_type="thread",
        chat_id="C123",
        channel_thread_id="1710000000.000001",
        kind="thread",
    )
    thread_id = thread.id
    earlier_handoff_id = uuid.UUID("00000000-0000-7000-8000-000000000001")
    later_handoff_id = uuid.UUID("00000000-0000-7000-8000-000000000002")
    sender = UserProfile(
        channel_tentacle_id="slack", channel_user_id="alice", name="Alice"
    )
    async with async_session() as session:
        session.add(sender)
        session.add(thread)
        session.add(
            ThreadMessage(
                thread_id=thread_id,
                platform_message_id="1710000000.000002",
                happened_at=datetime.now(UTC),
                direction="inbound",
                actor_kind="human",
                user_id="alice",
                sender_id=sender.id,
                sender=Relation(sender),
                segments=[TextSegment(data={"text": "handoff this"})],
                message_text="handoff this",
            )
        )
        session.add(
            Handoff(
                id=earlier_handoff_id,
                thread_id=thread_id,
                to_agent_tentacle_id="inkling",
                to_model="haiku",
                reason="Initial owner.",
                created_at=datetime(2030, 1, 1, tzinfo=UTC),
            )
        )
        session.add(
            Handoff(
                id=later_handoff_id,
                thread_id=thread_id,
                source_agent_tentacle_id="inkling",
                to_agent_tentacle_id="claude",
                to_model="sonnet",
                reason="Needs code work.",
                created_at=datetime(2020, 1, 1, tzinfo=UTC),
            )
        )
        await session.commit()

    async with async_session() as session:
        stored = await session.one_or_none(
            Thread,
            expressions=[
                Thread["channel_tentacle_id"] == "slack",
                Thread["chat_type"] == "thread",
                Thread["chat_id"] == "C123",
                Thread["channel_thread_id"] == "1710000000.000001",
            ],
        )
        assert stored is not None
        await stored.messages
        await stored.handoffs
        stored.status = "closed"
        await session.commit()

    async with async_session() as session:
        reloaded = await session.one_or_none(
            Thread,
            expressions=[Thread["id"] == stored.id],
        )
        assert reloaded is not None
        await reloaded.messages
        await reloaded.handoffs

    assert reloaded.status == "closed"
    assert reloaded.key == ThreadKey.from_address(_address(user_id="alice"))
    assert reloaded.messages[0].message_text == "handoff this"
    assert reloaded.active_agent_tentacle_id == "claude"
    assert reloaded.active_model == "sonnet"


async def test_thread_unique_key_has_no_user_id() -> None:
    async with async_session() as session:
        session.add(
            Thread(
                channel_tentacle_id="slack",
                chat_type="thread",
                chat_id="C123",
                channel_thread_id="1710000000.000001",
                kind="thread",
            )
        )
        session.add(
            Thread(
                channel_tentacle_id="slack",
                chat_type="thread",
                chat_id="C123",
                channel_thread_id="1710000000.000001",
                kind="thread",
            )
        )
        with pytest.raises(IntegrityError):
            await session.commit()


async def test_message_binding_round_trips_as_orm() -> None:
    thread = Thread(
        channel_tentacle_id="slack",
        chat_type="thread",
        chat_id="C123",
        channel_thread_id="1710000000.000001",
        kind="thread",
    )
    sender = UserProfile(
        channel_tentacle_id="slack", channel_user_id="alice", name="Alice"
    )
    thread_message = ThreadMessage(
        thread_id=thread.id,
        platform_message_id="1710000000.000002",
        happened_at=datetime.now(UTC),
        direction="inbound",
        actor_kind="human",
        user_id="alice",
        sender_id=sender.id,
        sender=Relation(sender),
        segments=[TextSegment(data={"text": "handoff this"})],
        message_text="handoff this",
    )
    async with async_session() as session:
        session.add(sender)
        session.add(thread)
        session.add(thread_message)
        await session.commit()

    manager = ConversationManager()
    conversation = await manager.ensure(
        thread.id,
        agent_tentacle_id="inkling",
    )
    run_id = "run-binding"
    await manager.record_agent_run(
        conversation,
        run_id=run_id,
        messages=[
            RawModelRequest(
                parts=[UserPromptPart(content="handoff this")],
                run_id=run_id,
                conversation_id=str(conversation.id),
                timestamp=datetime.now(UTC),
            )
        ],
    )
    model_message = (await manager.search_messages(conversation.id, "handoff"))[0]

    async with async_session() as session:
        session.add(
            MessageBinding(
                thread_message_id=thread_message.id,
                model_message_id=model_message.id,
                kind="request_source",
                run_id=run_id,
                position=0,
            )
        )
        await session.commit()

    async with async_session() as session:
        stored_message = await session.one_or_none(
            ThreadMessage,
            expressions=[ThreadMessage["id"] == thread_message.id],
        )
        stored_binding = await session.one_or_none(
            MessageBinding,
            expressions=[
                MessageBinding["thread_message_id"] == thread_message.id,
                MessageBinding["model_message_id"] == model_message.id,
                MessageBinding["kind"] == "request_source",
            ],
        )
        assert stored_message is not None
        assert stored_binding is not None
        await stored_message.model_messages

    assert stored_binding.run_id == run_id
    assert stored_message.model_messages[0].id == model_message.id
