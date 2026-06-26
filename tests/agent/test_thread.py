from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from datetime import datetime, timezone

import pytest
from pydantic_ai.messages import ModelRequest as RawModelRequest
from pydantic_ai.messages import UserPromptPart
from sqlalchemy import inspect
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine

from octomate.database import async_session
from octomate.managers import ConversationManager
from octomate.models import Base
from octomate.models.channel import ThreadMessage as ThreadMessageModel
from octomate.models.channel import MessageBinding as MessageBindingModel
from octomate.models.messages import ModelMessage as ModelMessageModel
from octomate.schemas.channel import (
    Handoff,
    ThreadMessage,
    Thread,
    ThreadKey,
    MessageBinding,
)
from octomate.schemas.conversation import ChannelAddress, UserProfile
from octomate.schemas.segments import TextSegment


@pytest.fixture(autouse=True)
async def _db(in_memory_engine: AsyncEngine) -> AsyncIterator[None]:
    yield


def _address(*, user_id: str) -> ChannelAddress:
    return ChannelAddress(
        channel_tentacle_id="slack",
        chat_type="group",
        chat_id="C123",
        user_id=user_id,
        thread_id="1710000000.000001",
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


def test_channel_handoff_sorts_by_uuid7_id() -> None:
    thread_id = uuid.UUID("00000000-0000-7000-8000-000000000100")
    earlier_handoff = Handoff(
        id=uuid.UUID("00000000-0000-7000-8000-000000000001"),
        thread_id=thread_id,
        to_agent_tentacle_id="inkling",
        created_at=datetime(2030, 1, 1, tzinfo=timezone.utc),
    )
    later_handoff = Handoff(
        id=uuid.UUID("00000000-0000-7000-8000-000000000002"),
        thread_id=thread_id,
        to_agent_tentacle_id="claude",
        created_at=datetime(2020, 1, 1, tzinfo=timezone.utc),
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
        chat_type="group",
        chat_id="C123",
        thread_id="1710000000.000001",
    )
    thread_id = thread.id
    earlier_handoff_id = uuid.UUID("00000000-0000-7000-8000-000000000001")
    later_handoff_id = uuid.UUID("00000000-0000-7000-8000-000000000002")
    async with async_session() as session:
        session.add(thread)
        session.add(
            ThreadMessage(
                thread_id=thread_id,
                platform_message_id="1710000000.000002",
                timestamp=datetime.now(timezone.utc),
                direction="inbound",
                actor_kind="human",
                user_id="alice",
                sender=UserProfile(user_id="alice", name="Alice"),
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
                created_at=datetime(2030, 1, 1, tzinfo=timezone.utc),
            )
        )
        session.add(
            Handoff(
                id=later_handoff_id,
                thread_id=thread_id,
                from_agent_tentacle_id="inkling",
                to_agent_tentacle_id="claude",
                to_model="sonnet",
                reason="Needs code work.",
                created_at=datetime(2020, 1, 1, tzinfo=timezone.utc),
            )
        )
        await session.commit()

    async with async_session() as session:
        stored = await session.one_or_none(
            Thread,
            expressions=[
                Thread["channel_tentacle_id"] == "slack",
                Thread["chat_type"] == "group",
                Thread["chat_id"] == "C123",
                Thread["thread_id"] == "1710000000.000001",
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
                chat_type="group",
                chat_id="C123",
                thread_id="1710000000.000001",
            )
        )
        session.add(
            Thread(
                channel_tentacle_id="slack",
                chat_type="group",
                chat_id="C123",
                thread_id="1710000000.000001",
            )
        )
        with pytest.raises(IntegrityError):
            await session.commit()


async def test_message_binding_round_trips_as_orm() -> None:
    thread = Thread(
        channel_tentacle_id="slack",
        chat_type="group",
        chat_id="C123",
        thread_id="1710000000.000001",
    )
    thread_message = ThreadMessage(
        thread_id=thread.id,
        platform_message_id="1710000000.000002",
        timestamp=datetime.now(timezone.utc),
        direction="inbound",
        actor_kind="human",
        user_id="alice",
        sender=UserProfile(user_id="alice", name="Alice"),
        segments=[TextSegment(data={"text": "handoff this"})],
        message_text="handoff this",
    )
    async with async_session() as session:
        session.add(thread)
        session.add(thread_message)
        await session.commit()

    manager = ConversationManager()
    conversation = await manager.ensure(
        _address(user_id="alice"),
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
                timestamp=datetime.now(timezone.utc),
            )
        ],
    )
    model_message = (await manager.search_messages(conversation.id, "handoff"))[0]

    async with async_session() as session:
        session.add(
            MessageBinding(
                thread_message_id=thread_message.id,
                model_message_id=model_message.id,
                kind="prompt_source",
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
                MessageBinding["kind"] == "prompt_source",
            ],
        )
        assert stored_message is not None
        assert stored_binding is not None
        await stored_message.model_messages

    assert stored_binding.run_id == run_id
    assert stored_message.model_messages[0].id == model_message.id
