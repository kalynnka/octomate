"""Command receipts share visible history but never become pending chat input."""

import pytest
from pydantic import ValidationError
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine

from octomate.database import async_session
from octomate.managers import ConversationManager, ThreadManager, UserManager
from octomate.schemas.commands import CommandError, CommandInvocation, CommandResult
from octomate.schemas.conversation import Conversation
from octomate.schemas.events import MessageEvent
from octomate.schemas.segments import FileData, FileSegment, TextSegment
from octomate.schemas.thread import ThreadCommand, ThreadMessage
from octomate.schemas.user import UserProfile


@pytest.fixture
async def receipt(in_memory_engine: AsyncEngine) -> ThreadCommand:
    threads = ThreadManager(users=UserManager())
    opening = await threads.record_inbound(
        MessageEvent(
            tentacle_id="im",
            chat_type="group",
            chat_id="chat",
            user_id="alice",
            message_id="chat-before",
            sender=UserProfile(channel_user_id="alice"),
            segments=[TextSegment(data={"text": "Keep this pending"})],
        )
    )
    conversation = await ConversationManager().ensure(
        opening.thread_id, agent_tentacle_id="codex"
    )
    return ThreadCommand(
        thread_id=opening.thread_id,
        direction="inbound",
        actor_kind="human",
        user_id="alice",
        sender_id=opening.sender_id,
        agent_tentacle_id="codex",
        platform_message_id="command-1",
        conversation_id=conversation.id,
        invocation=CommandInvocation(
            command_id="review",
            arguments='  "raw argument"\n--flag=✓  ',
            attachments=[FileSegment(data=FileData(file="/resolved/input.txt"))],
        ),
        message_text="/review",
    )


@pytest.mark.parametrize(
    "outcome",
    [
        CommandResult(segments=[TextSegment(data={"text": "Done"})]),
        CommandError(status="failed", message="Effects may already have occurred."),
    ],
)
async def test_command_receipt_and_outcome_roundtrip(
    receipt: ThreadCommand, outcome: CommandResult | CommandError
) -> None:
    async with async_session() as session:
        session.add(receipt)
        await session.commit()
    async with async_session() as session:
        stored = await session.one_or_none(
            ThreadMessage, expressions=[ThreadMessage["id"] == receipt.id]
        )
        assert isinstance(stored, ThreadCommand)
        assert stored.invocation == receipt.invocation
        assert stored.conversation_id == receipt.conversation_id
        assert stored.outcome is None
        stored.outcome = outcome
        await session.commit()
    threads = ThreadManager(users=UserManager())
    delivery = await threads.find_message(receipt.thread_id, "command-1", "inbound")
    assert isinstance(delivery, ThreadCommand)
    assert delivery.outcome == outcome
    assert delivery.model_messages == []


async def test_commands_stay_in_history_without_consuming_pending_chat(
    receipt: ThreadCommand,
) -> None:
    async with async_session() as session:
        session.add(receipt)
        await session.commit()
    threads = ThreadManager(users=UserManager())
    thread = await threads.get(receipt.thread_id)
    assert thread is not None
    assert [message.kind for message in thread.messages] == ["message", "command"]
    assert (
        thread.model_dump()["messages"][1]["invocation"]
        == receipt.invocation.model_dump()
    )
    assert [
        message.platform_message_id
        for message in await threads.chat_messages_before(thread.id, receipt.id)
    ] == ["chat-before"]
    assert await threads.chat_messages_after(thread.id, receipt.id) == []
    for agent_id in ("codex", "claude"):
        pending = await threads.pending_prompt_messages(thread, receipt.id, agent_id)
        assert [message.platform_message_id for message in pending] == ["chat-before"]
    assert thread.source_cursor_message_id is None


async def test_deleted_conversation_keeps_the_command_receipt(
    receipt: ThreadCommand,
) -> None:
    async with async_session() as session:
        session.add(receipt)
        await session.commit()
        conversation = await session.get(Conversation, receipt.conversation_id)
        assert conversation is not None
        await session.delete(conversation)
        await session.commit()
    async with async_session() as session:
        stored = await session.get(ThreadCommand, receipt.id)
        assert stored is not None
        assert stored.conversation_id is None
        assert stored.invocation == receipt.invocation


async def test_delivery_constraint_rejects_a_second_receipt(
    receipt: ThreadCommand,
) -> None:
    async with async_session() as session:
        session.add(receipt)
        await session.commit()
    duplicate = ThreadCommand.model_validate(receipt.model_dump(exclude={"id"}))
    async with async_session() as session:
        session.add(duplicate)
        with pytest.raises(IntegrityError):
            await session.commit()


@pytest.mark.parametrize("delivery_id", [None, ""])
def test_command_receipts_require_a_delivery_id(
    receipt: ThreadCommand, delivery_id: str | None
) -> None:
    with pytest.raises(ValidationError, match="require a platform_message_id"):
        ThreadCommand.model_validate(
            receipt.model_dump() | {"platform_message_id": delivery_id}
        )
