"""A fork points at its source's stored history instead of copying it, and each
side's history grows on its own after."""

from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import Mock

import pytest
from pydantic_ai.messages import (
    ModelRequest,
    ModelResponse,
    TextPart,
    ToolCallPart,
    ToolReturnPart,
    UserPromptPart,
)
from sqlalchemy import delete, event
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine
from uuid_utils.compat import uuid7

from octomate.database import async_session
from octomate.managers.conversation import ConversationManager
from octomate.managers.thread import ThreadManager
from octomate.managers.user import UserManager
from octomate.schemas.commands import CommandInvocation, CommandResult
from octomate.schemas.conversation import Conversation
from octomate.schemas.messages import ModelMessage
from octomate.schemas.runs import AgentRun
from octomate.schemas.segments import FileData, FileSegment, ReplySegment, TextSegment
from octomate.schemas.thread import (
    Handoff,
    MessageBinding,
    Thread,
    ThreadCommand,
    ThreadMessage,
)
from octomate.schemas.user import UserProfile
from tests.support.managers import a_thread


@pytest.fixture(autouse=True)
async def database(in_memory_engine: AsyncEngine) -> None:
    return


@pytest.mark.parametrize(("copies", "subthread"), [(1, False), (2, False), (1, True)])
async def test_fork_shares_the_source_history_and_grows_apart(
    copies: int,
    subthread: bool,
    in_memory_engine: AsyncEngine,
) -> None:
    manager = ConversationManager()
    surface_id = await a_thread("source")
    threads = ThreadManager(users=UserManager())
    surface = await threads.get(surface_id)
    assert surface is not None
    thread = await threads.open_sub_thread(surface) if subthread else surface
    source = await manager.ensure(thread.id, agent_tentacle_id="codex")
    other = await manager.ensure(source.thread_id, agent_tentacle_id="claude")
    child = await manager.ensure(
        source.thread_id,
        agent_tentacle_id="codex",
        subagent_id="helper",
        parent_conversation_id=source.id,
    )
    first = await manager.record_agent_run(
        source,
        str(uuid7()),
        [
            ModelRequest(parts=[UserPromptPart("Alice #msg:question: radio check")]),
            ModelResponse(parts=[TextPart("Loud and clear")], model_name="model-a"),
        ],
        name="first",
        model_name="model-a",
        permission_mode="ask",
        cwd=Path("/workspace"),
        external_id="source-session",
        native_id="codex-native",
        native_turn_id="source-turn",
    )
    assert first is not None
    second = await manager.record_agent_run(
        source,
        str(uuid7()),
        [ModelRequest(parts=[UserPromptPart("Bob: follow up")])],
        name="second",
        model_name="model-b",
    )
    assert second is not None
    await manager.record_agent_run(
        child,
        str(uuid7()),
        [ModelResponse(parts=[TextPart("Helper answer")])],
        parent_run_id=first.id,
        parent_tool_call_id="helper-call",
    )
    await manager.record_agent_run(
        other,
        str(uuid7()),
        [ModelResponse(parts=[TextPart("Other agent history")])],
        external_id="other-session",
    )
    alice = UserProfile(
        channel_tentacle_id="test", channel_user_id="alice", name="Alice"
    )
    bob = UserProfile(channel_tentacle_id="test", channel_user_id="bob", name="Bob")
    question = ThreadMessage(
        thread_id=surface_id,
        platform_message_id="question",
        direction="inbound",
        actor_kind="human",
        sender_id=alice.id,
        user_id="alice",
        message_text="radio check",
        raw="original payload",
        segments=[
            TextSegment(data={"text": "radio check"}),
            FileSegment(
                data=FileData(file="/files/brief.txt", name="brief.txt", size=12)
            ),
        ],
    )
    answer = ThreadMessage(
        thread_id=surface_id,
        platform_message_id="answer",
        reply_id="question",
        direction="outbound",
        actor_kind="agent",
        sender_id=alice.id,
        agent_tentacle_id="codex",
        message_text="Loud and clear",
        segments=[
            ReplySegment(data={"id": "question"}),
            TextSegment(data={"text": "Loud and clear"}),
        ],
    )
    followup = ThreadMessage(
        thread_id=surface_id,
        direction="inbound",
        actor_kind="human",
        sender_id=bob.id,
        user_id="bob",
        message_text="follow up",
    )
    receipt = ThreadCommand(
        thread_id=surface_id,
        platform_message_id="command",
        direction="inbound",
        actor_kind="human",
        sender_id=bob.id,
        conversation_id=source.id,
        invocation=CommandInvocation(command_id="builtin:status"),
        outcome=CommandResult(),
    )
    bindings = [
        MessageBinding(
            thread_message_id=question.id,
            model_message_id=first.messages[0].id,
            run_id=first.id,
            kind="request_source",
        ),
        MessageBinding(
            thread_message_id=answer.id,
            model_message_id=first.messages[1].id,
            run_id=first.id,
            kind="assistant_reply",
            tool_call_id="reply-call",
            position=1,
        ),
        MessageBinding(
            thread_message_id=followup.id,
            model_message_id=second.messages[0].id,
            run_id=second.id,
            kind="request_source",
        ),
    ]
    async with async_session() as session:
        session.add(alice)
        session.add(bob)
        await session.flush()
        for message in (question, answer, followup, receipt):
            session.add(message)
        await session.flush()
        for binding in bindings:
            session.add(binding)
        session.add(
            Handoff(
                thread_id=source.thread_id,
                source_agent_tentacle_id="claude",
                to_agent_tentacle_id="codex",
                source_conversation_id=other.id,
                target_conversation_id=source.id,
                source_run_id=first.id,
                source_model_message_id=first.messages[1].id,
                reason="Take over",
            )
        )
        stored = await session.get(Conversation, source.id)
        assert stored is not None
        stored.name, stored.effort, stored.allowed_tools = (
            "Named context",
            "high",
            ["read"],
        )
        thread = await session.get(Thread, source.thread_id)
        assert thread is not None
        thread.source_cursor_message_id = followup.id
        await session.commit()

    original = await manager.get(source.id)
    for index in range(copies):
        target = await manager.ensure(
            await a_thread(f"copy-{index}"), agent_tentacle_id="codex"
        )
        queries = Mock()
        event.listen(in_memory_engine.sync_engine, "before_cursor_execute", queries)
        try:
            await manager.fork(source, target, external_id=f"copied-session-{index}")
        finally:
            event.remove(in_memory_engine.sync_engine, "before_cursor_execute", queries)
        statements = [call.args[2] for call in queries.call_args_list]
        for statement in statements:
            assert isinstance(statement, str)
            if statement.startswith("SELECT"):
                assert "model_messages.parts" not in statement
                assert "thread_messages.segments" not in statement
                assert "thread_messages.invocation" not in statement
                assert "message_binding.kind" not in statement
        # Nothing is copied: the fork writes only rows pointing at the history.
        assert {
            statement.split()[2]
            for statement in statements
            if statement.startswith("INSERT")
        } == (
            {"conversation_runs"}
            if subthread
            else {"conversation_runs", "thread_ledgers"}
        )
        source = await manager.get(target.id)
    forked = source
    assert forked.external_id == f"copied-session-{copies - 1}"
    assert (forked.name, forked.effort, forked.allowed_tools) == (
        "Named context",
        "high",
        ["read"],
    )
    assert [run.id for run in forked.runs] == [first.id, second.id]
    assert [message.id for message in forked.messages] == [
        message.id for message in original.messages
    ]
    contexts = await manager.for_thread(forked.thread_id)
    assert [context.id for context in contexts] == [forked.id]
    landed = await threads.get(forked.thread_id)
    assert landed is not None
    if subthread:
        assert not landed.messages
        assert landed.source_cursor_message_id is None
    else:
        assert [message.id for message in landed.messages] == [
            question.id,
            answer.id,
            followup.id,
            receipt.id,
        ]
        assert (await landed.messages[0].sender).name == "Alice"
        assert landed.source_cursor_message_id == followup.id
    assert not landed.handoffs

    # From here each side's history grows on its own.
    await manager.record_agent_run(
        forked,
        str(uuid7()),
        [ModelRequest(parts=[UserPromptPart("only in the fork")])],
    )
    aside = ThreadMessage(
        thread_id=forked.thread_id,
        direction="inbound",
        actor_kind="human",
        sender_id=bob.id,
        user_id="bob",
        message_text="only in the fork",
    )
    async with async_session() as session:
        session.add(aside)
        await session.commit()
    assert len((await manager.get(forked.id)).runs) == 3
    assert [run.id for run in (await manager.get(original.id)).runs] == [
        first.id,
        second.id,
    ]
    landed = await threads.get(forked.thread_id)
    assert landed is not None
    assert landed.messages[-1].id == aside.id
    surface = await threads.get(surface_id)
    assert surface is not None
    assert aside.id not in {message.id for message in surface.messages}


@pytest.mark.parametrize("bound", [True, False])
async def test_native_fork_leaves_unfinished_turns_behind(bound: bool) -> None:
    manager = ConversationManager()
    source = await manager.ensure(
        await a_thread("native"), agent_tentacle_id="claude-native"
    )
    target = await manager.ensure(await a_thread("driven"), agent_tentacle_id="claude")
    sender = UserProfile(
        channel_tentacle_id="claude-native", channel_user_id="alice", name="Alice"
    )
    async with async_session() as session:
        session.add(sender)
        await session.commit()
    recorded = []
    for offset, text in ((10, "completed"), (None, "pending")):
        timestamp = datetime(2026, 1, 1, tzinfo=UTC)
        request = ModelRequest(parts=[UserPromptPart(text)], timestamp=timestamp)
        run = await manager.record_external_run(
            source,
            str(uuid7()),
            [request],
            native_session_id="native-session",
            end_offset=offset,
        )
        assert run is not None
        recorded.append(run)
        message = ThreadMessage(
            thread_id=source.thread_id,
            platform_message_id=run.id,
            direction="inbound",
            actor_kind="human",
            sender_id=sender.id,
            message_text=text,
            happened_at=timestamp,
        )
        async with async_session() as session:
            session.add(message)
            await session.flush()
            if bound:
                session.add(
                    MessageBinding(
                        thread_message_id=message.id,
                        model_message_id=run.messages[0].id,
                        run_id=run.id,
                        kind="request_source",
                    )
                )
            await session.commit()
    async with async_session() as session:
        session.add(
            Handoff(
                thread_id=source.thread_id,
                to_agent_tentacle_id="claude-native",
                target_conversation_id=source.id,
                created_at=datetime(2026, 1, 1, tzinfo=UTC),
            )
        )
        await session.commit()
    await manager.fork(source, target, external_id="forked-session", end_offset=10)
    forked = await manager.get(target.id)
    assert [run.id for run in forked.runs] == [recorded[0].id]
    landed = await ThreadManager(users=UserManager()).get(target.thread_id)
    assert landed is not None
    assert [message.message_text for message in landed.messages] == ["completed"]
    # The source's handoffs stay with it; the fork's own conversation owns the fork.
    assert landed.latest_handoff is None
    assert landed.active_agent_tentacle_id == "claude"


async def test_abandoning_a_shared_tool_call_leaves_the_fork_resuming_it() -> None:
    manager = ConversationManager()
    source = await manager.ensure(await a_thread("source"), agent_tentacle_id="inkling")
    pending = await manager.record_agent_run(
        source,
        str(uuid7()),
        [
            ModelRequest(parts=[UserPromptPart("take this to a thread")]),
            ModelResponse(parts=[ToolCallPart("teleport", {}, tool_call_id="move")]),
        ],
    )
    assert pending is not None
    target = await manager.ensure(await a_thread("target"), agent_tentacle_id="inkling")
    await manager.fork(source, target)

    denial = ToolReturnPart("teleport", "Not carried out", tool_call_id="move")
    prompt = UserPromptPart("never mind")
    await manager.record_agent_run(
        source,
        str(uuid7()),
        [ModelRequest(parts=[denial, prompt])],
    )
    source = await manager.get(source.id)
    assert [message.id for message in source.messages[:-1]] == [
        message.id for message in pending.messages
    ]
    assert source.messages[-1].parts == [denial, prompt]
    forked = await manager.get(target.id)
    assert [message.id for message in forked.messages] == [
        message.id for message in pending.messages
    ]


@pytest.mark.parametrize("removal", ["conversation", "thread", "run"])
async def test_a_run_a_fork_includes_cannot_be_deleted(removal: str) -> None:
    manager = ConversationManager()
    source = await manager.ensure(await a_thread("source"), agent_tentacle_id="codex")
    run = await manager.record_agent_run(
        source, str(uuid7()), [ModelRequest(parts=[UserPromptPart("keep this")])]
    )
    assert run is not None
    target = await manager.ensure(await a_thread("target"), agent_tentacle_id="codex")
    await manager.fork(source, target)

    statements = {
        "conversation": delete(Conversation).where(Conversation["id"] == source.id),
        "thread": delete(Thread).where(Thread["id"] == source.thread_id),
        "run": delete(AgentRun).where(AgentRun["id"] == run.id),
    }
    async with async_session() as session:
        with pytest.raises(IntegrityError):
            await session.execute(statements[removal])

    for conversation in (source, target):
        history = await manager.get(conversation.id)
        assert [message.id for message in history.messages] == [run.messages[0].id]


async def test_a_source_deletes_with_its_runs_once_no_fork_includes_them() -> None:
    manager = ConversationManager()
    source = await manager.ensure(await a_thread("source"), agent_tentacle_id="codex")
    run = await manager.record_agent_run(
        source, str(uuid7()), [ModelRequest(parts=[UserPromptPart("keep this")])]
    )
    assert run is not None
    target = await manager.ensure(await a_thread("target"), agent_tentacle_id="codex")
    await manager.fork(source, target)

    async with async_session() as session:
        await session.execute(
            delete(Conversation).where(Conversation["id"] == target.id)
        )
        await session.commit()
    history = await manager.get(source.id)
    assert [message.id for message in history.messages] == [run.messages[0].id]

    async with async_session() as session:
        await session.execute(
            delete(Conversation).where(Conversation["id"] == source.id)
        )
        await session.commit()
        assert await session.get(AgentRun, run.id) is None
        assert await session.list(ModelMessage, limit=None) == []
