"""dsh completion records survive every producer and never invent a turn."""

import asyncio
from pathlib import Path
from unittest.mock import Mock

import pytest
from arcanus.materia.sqlalchemy import selectinload
from pydantic_ai.exceptions import AgentRunError
from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncEngine
from uuid_utils.compat import uuid7

from octomate import Octomate
from octomate.config.agents import DeepseekConfig
from octomate.database import async_session
from octomate.managers.conversation import ConversationManager
from octomate.schemas.commands import CommandContext, CommandResult
from octomate.schemas.compactions import Compaction, CompactionMembership
from octomate.schemas.conversation import ConversationCompaction
from octomate.tentacles.deepseek import DeepseekTentacle
from octomate.tentacles.deepseek.adapter import DeepseekRunAccumulator
from octomate.tentacles.deepseek.compactions import DeepseekCompactions
from octomate.tentacles.deepseek.ink import TurnFrame
from octomate.tentacles.deepseek.wire import (
    AssistantStreamChunk,
    CommandExecutionValue,
    SessionAssistantFrame,
    SessionEvent,
)
from octomate.types.json import JsonObject
from tests.agent.deepseek.test_native_ingest import (
    LOG_LABEL,
    SENDER,
    SESSION_ADDRESS,
    SESSION_ID,
    ev,
    feed_events,
    native_conversation,
    stream_events,
    turn_events,
    user_message,
    wired,
)
from tests.agent.deepseek.test_tentacle import (
    KEY,
    FakeDeepseekApi,
    patch_gateway,
    session_event,
)
from tests.support.managers import a_thread


def compaction_events(
    *,
    manual: bool = True,
    failed: bool = False,
    base: int = 5,
    shadowed: list[int] | None = None,
) -> list[JsonObject]:
    shadowed = [1, 2] if shadowed is None else shadowed
    lifecycle: JsonObject = {
        "compactionId": str(uuid7()),
        "turn": None if manual else 1,
    }
    if manual:
        lifecycle["sourceCommandId"] = "cmd-1"
    return [
        ev(base, "compaction/start", lifecycle),
        ev(
            base + 1,
            "compaction/summary",
            {
                **lifecycle,
                "summary": [{"type": "text", "text": "Earlier work summarized"}],
                "provider": "deepseek",
                "model": "deepseek-v4-flash",
                "shadowedRange": {"start": shadowed[0], "end": shadowed[-1]},
                "shadowedSeqs": [*shadowed],
                "shadowedTokenCount": 10000,
            },
        ),
        ev(
            base + 2,
            "user/message",
            {
                "role": "user",
                "content": [
                    {
                        "type": "text",
                        "text": "<summary>Earlier work summarized</summary>",
                    }
                ],
                "source": {
                    "kind": "compact-checkpoint",
                    "compactionId": lifecycle["compactionId"],
                },
            },
        ),
        ev(
            base + 3,
            "compaction/end",
            {**lifecycle, **({"error": "cancelled"} if failed else {})},
        ),
    ]


@pytest.mark.parametrize("manual", [False, True])
async def test_native_compactions_between_turns_and_replay_preserve_archive(
    in_memory_engine: AsyncEngine,
    manual: bool,
) -> None:
    app = Octomate()
    _, tailer = wired(app)
    records = [
        *turn_events(1, 0, "first", "one"),
        *compaction_events(manual=manual),
        *turn_events(2, 9, "second", "two"),
    ]
    for _ in range(2):
        await stream_events(tailer, app.nerve, records)
    conversation = await native_conversation(app)
    assert len(conversation.runs) == 2
    assert [
        m.message_text for m in (await ConversationManager().history(conversation.id))
    ] == [
        "first",
        "one",
        "second",
        "two",
    ]
    async with async_session() as session:
        [row] = await session.list(
            Compaction, options=[selectinload(Compaction["messages"])]
        )
    assert row.native_session_id == SESSION_ID
    assert row.conversation_id == conversation.id
    assert row.trigger == ("manual" if manual else "auto")
    [message] = row.messages
    assert message.message_text == "<summary>Earlier work summarized</summary>"
    assert message.run_id is None
    assert message.compaction_id == row.id
    assert message.metadata is not None
    assert message.metadata["events"]
    assert row.membership_complete
    effective = await app.conversations.effective_messages(conversation)
    assert [m.message_text for m in effective] == [
        message.message_text,
        "second",
        "two",
    ]
    async with async_session() as session:
        assert await session.count(CompactionMembership) == 3
        assert await session.count(ConversationCompaction) == 1


@pytest.mark.parametrize("stage", ["started", "summary", "failed", "malformed"])
async def test_unfinished_or_failed_compaction_never_persists(
    in_memory_engine: AsyncEngine,
    stage: str,
) -> None:
    app = Octomate()
    _, tailer = wired(app)
    records = compaction_events(failed=stage == "failed")
    if stage == "started":
        records = records[:1]
    elif stage == "summary":
        records = records[:3]
    elif stage == "malformed":
        records[1]["data"] = {"compactionId": "broken"}
    await stream_events(tailer, app.nerve, records)
    async with async_session() as session:
        assert await session.count(Compaction) == 0
    assert not (await native_conversation(app)).runs


@pytest.mark.parametrize("with_history", [False, True])
async def test_driven_command_compaction_has_summary_without_a_model_run(
    in_memory_engine: AsyncEngine,
    with_history: bool,
) -> None:
    app = Octomate()
    thread = await app.threads.enter(KEY)
    conversation = await app.conversations.ensure(
        thread.id, agent_tentacle_id="deepseek"
    )
    await app.conversations.set_external_id(conversation, "sess-1")
    if with_history:
        accumulator = DeepseekRunAccumulator()
        accumulator.begin("first")
        for raw in turn_events(1, 0, "first", "one"):
            if raw["type"] == "assistant/message":
                list(
                    accumulator.consume_assistant_stream(
                        SessionAssistantFrame(
                            type="assistant-stream",
                            frame=AssistantStreamChunk(
                                type="chunk",
                                attempt_id=str(uuid7()),
                                revision=0,
                                index=0,
                                time=0,
                                chunk={"type": "text-delta", "text": "one"},
                            ),
                        )
                    )
                )
            list(accumulator.consume(session_event("sess-1", raw)))
        await app.conversations.record_agent_run(
            conversation, str(uuid7()), accumulator.messages, external_id="sess-1"
        )
    agent = DeepseekTentacle(
        app, auth=app.auth, habitats=app.habitats, config=DeepseekConfig()
    )
    queue: asyncio.Queue[TurnFrame] = asyncio.Queue()
    records = compaction_events()
    for raw in [
        ev(0, "command/run", {"commandId": "cmd-1"}),
        *records,
        ev(9, "command/done", {"commandId": "cmd-1"}),
    ]:
        queue.put_nowait(session_event("sess-1", raw))
    outcomes = [
        item
        async for item in agent.command_events(
            CommandContext(
                agent_id=agent.id,
                user_id=uuid7(),
                address=KEY,
                cwd=Path("/repo"),
                conversation=conversation,
            ),
            CommandExecutionValue.model_validate(
                {"commandId": "cmd-1", "result": {"kind": "success"}}
            ),
            queue,
        )
    ]
    assert len(outcomes) == 1
    assert isinstance(outcomes[0], CommandResult)
    # A repeated observation uses the stable compaction identity.
    tracker = DeepseekCompactions(app.conversations, conversation, "sess-1")
    for raw in records:
        await tracker.consume(SessionEvent.model_validate(raw))
    await tracker.record_memberships()
    async with async_session() as session:
        [row] = await session.list(
            Compaction, options=[selectinload(Compaction["messages"])]
        )
    assert row.kind == "octomate"
    assert row.messages[0].message_text == "<summary>Earlier work summarized</summary>"
    assert len((await app.conversations.get(conversation.id)).runs) == int(with_history)
    assert row.membership_complete is with_history
    if with_history:
        assert len(await app.conversations.effective_messages(conversation)) == 1


@pytest.mark.parametrize("failed_run", [False, True])
async def test_automatic_driven_compaction_survives_the_enclosing_run(
    in_memory_engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
    failed_run: bool,
) -> None:
    patch_gateway(monkeypatch)
    records = turn_events(
        1, 0, "work", "done", reason="error" if failed_run else "completed"
    )
    records[2]["seq"] = 9
    records[3]["seq"] = 10
    FakeDeepseekApi.reset(
        [*records[:2], *compaction_events(manual=False, shadowed=[1]), *records[2:]]
    )
    app = Octomate()
    thread = await app.threads.enter(KEY)
    agent = DeepseekTentacle(
        app, auth=app.auth, habitats=app.habitats, config=DeepseekConfig()
    )
    async with agent:
        if failed_run:
            with pytest.raises(AgentRunError):
                await agent.run(
                    "work",
                    conversation_address=KEY,
                    conversation=await agent.resolve_conversation(
                        thread.id, conversation_address=KEY
                    ),
                )
        else:
            await agent.run(
                "work",
                conversation_address=KEY,
                conversation=await agent.resolve_conversation(
                    thread.id, conversation_address=KEY
                ),
            )
    async with async_session() as session:
        [row] = await session.list(
            Compaction, options=[selectinload(Compaction["messages"])]
        )
    assert row.trigger == "auto"
    assert row.messages[0].message_text == "<summary>Earlier work summarized</summary>"
    assert row.membership_complete
    context = await app.conversations.get(row.conversation_id, with_messages=True)
    assert [message.message_text for message in context.messages] == [
        row.messages[0].message_text,
        "done",
    ]


async def test_native_membership_retains_tail_and_is_shared_by_forks(
    in_memory_engine: AsyncEngine,
) -> None:
    app = Octomate()
    _, tailer = wired(app)
    records = [
        *turn_events(1, 0, "first", "one"),
        *turn_events(2, 4, "keep", "two"),
        *compaction_events(base=9),
    ]
    await stream_events(tailer, app.nerve, records)
    source = await native_conversation(app)
    archive = await app.conversations.history(source.id)
    effective = await app.conversations.effective_messages(source)
    assert [message.id for message in effective[1:]] == [
        message.id for message in archive[2:]
    ]
    fork = await app.conversations.ensure(
        await a_thread("fork"), agent_tentacle_id="deepseek"
    )
    await app.conversations.fork(source, fork, external_id="fork-session")
    assert [
        message.id for message in await app.conversations.effective_messages(fork)
    ] == [message.id for message in effective]
    await stream_events(
        tailer, app.nerve, [*records, *turn_events(3, 14, "next", "three")]
    )
    assert len(await app.conversations.effective_messages(source)) == 5
    assert len(await app.conversations.effective_messages(fork)) == 3
    async with async_session() as session:
        assert await session.count(Compaction) == 1
        assert await session.count(CompactionMembership) == 7
        assert await session.count(ConversationCompaction) == 2


async def test_multiple_in_turn_compactions_wait_for_durable_messages(
    in_memory_engine: AsyncEngine,
) -> None:
    app = Octomate()
    _, tailer = wired(app)
    first = turn_events(1, 0, "first", "one")
    later = turn_events(1, 7, "unused", "two")
    later[3]["seq"] = 16
    records = [
        *first[:3],
        *compaction_events(manual=False),
        later[2],
        *compaction_events(manual=False, base=12, shadowed=[7, 9]),
    ]
    await stream_events(tailer, app.nerve, records)
    source = await native_conversation(app)
    async with async_session() as session:
        assert await session.count(CompactionMembership) == 0
    # Reconnect with the complete turn: both boundaries now refer to saved UUIDs.
    await stream_events(tailer, app.nerve, [*records, later[3]])
    async with async_session() as session:
        rows = await session.list(Compaction)
        assert len(rows) == 2
        assert all(row.membership_complete for row in rows)
        assert await session.count(CompactionMembership) == 6
    assert len(await app.conversations.history(source.id)) == 3
    assert len(await app.conversations.effective_messages(source)) == 1


async def test_in_turn_compaction_includes_uncommitted_retained_prompt(
    in_memory_engine: AsyncEngine,
) -> None:
    app = Octomate()
    _, tailer = wired(app)
    await stream_events(tailer, app.nerve, turn_events(1, 0, "first", "one"))
    current = turn_events(2, 4, "retain", "two")
    current[2]["seq"] = 13
    current[3]["seq"] = 14
    async with app.nerve.publish(SESSION_ADDRESS) as impulses:
        state, _ = await tailer.attach_remote(
            SESSION_ID, LOG_LABEL, "/repo", SENDER, SESSION_ADDRESS, impulses
        )
        await feed_events(
            tailer, state, [*current[:2], *compaction_events(base=7, manual=False)]
        )
        async with async_session() as session:
            assert await session.count(CompactionMembership) == 0
        await feed_events(tailer, state, current[2:])
        tailer.detach_remote(state)
    conversation = await native_conversation(app)
    archive = await app.conversations.history(conversation.id)
    async with async_session() as session:
        before = await session.list(
            CompactionMembership,
            expressions=[CompactionMembership["stage"] == "before"],
            order_bys=[CompactionMembership["position"]],
        )
    assert [member.model_message_id for member in before] == [
        message.id for message in archive[:3]
    ]
    assert [
        message.message_text
        for message in await app.conversations.effective_messages(conversation)
    ] == ["<summary>Earlier work summarized</summary>", "retain", "two"]


async def test_no_compaction_does_not_load_or_calculate_membership(
    in_memory_engine: AsyncEngine,
) -> None:
    app = Octomate()
    thread = await app.threads.enter(KEY)
    conversation = await app.conversations.ensure(
        thread.id, agent_tentacle_id="deepseek"
    )
    tracker = DeepseekCompactions(app.conversations, conversation, "session")
    queries = Mock()
    event.listen(in_memory_engine.sync_engine, "before_cursor_execute", queries)
    try:
        for raw in turn_events(1, 0, "first", "one"):
            await tracker.consume(SessionEvent.model_validate(raw))
        await tracker.record_memberships()
    finally:
        event.remove(in_memory_engine.sync_engine, "before_cursor_execute", queries)
    assert queries.call_count == 0


@pytest.mark.parametrize("whole_pair", [False, True])
async def test_compaction_cannot_split_a_normalized_tool_pair(
    in_memory_engine: AsyncEngine,
    whole_pair: bool,
) -> None:
    app = Octomate()
    _, tailer = wired(app)
    await stream_events(
        tailer,
        app.nerve,
        [
            *turn_events(1, 0, "list files", "unused")[:2],
            ev(
                2,
                "assistant/message",
                {
                    "message": {
                        "role": "assistant",
                        "content": [
                            {
                                "type": "tool-call",
                                "id": "c1",
                                "name": "bash",
                                "arguments": '{"command":"ls"}',
                            }
                        ],
                    }
                },
            ),
            ev(
                3,
                "tool/call",
                {"callId": "c1", "name": "bash", "arguments": '{"command":"ls"}'},
            ),
            ev(
                4,
                "tool/result",
                {
                    "message": {
                        "role": "tool",
                        "toolCallId": "c1",
                        "content": [{"type": "text", "text": "a.txt"}],
                        "isError": False,
                    }
                },
            ),
            ev(5, "turn/end", {"turn": 1, "reason": {"kind": "completed"}}),
            *compaction_events(base=7, shadowed=[1, 2, 4] if whole_pair else [1, 2]),
        ],
    )
    conversation = await native_conversation(app)
    assert len(await app.conversations.history(conversation.id)) == 2
    async with async_session() as session:
        [row] = await session.list(Compaction)
    assert row.membership_complete is whole_pair
    if whole_pair:
        assert len(await app.conversations.effective_messages(conversation)) == 1
    else:
        with pytest.raises(ValueError, match="every compaction to be linked"):
            await app.conversations.effective_messages(conversation)


@pytest.mark.parametrize("shadowed", [[1, 99], [2, 1], [1, 6]])
async def test_unmapped_or_noncontiguous_surface_stays_unknown(
    in_memory_engine: AsyncEngine,
    shadowed: list[int],
) -> None:
    app = Octomate()
    _, tailer = wired(app)
    await stream_events(
        tailer,
        app.nerve,
        [
            *turn_events(1, 0, "first", "one"),
            *turn_events(2, 4, "second", "two"),
            *compaction_events(base=9, shadowed=shadowed),
        ],
    )
    conversation = await native_conversation(app)
    async with async_session() as session:
        [row] = await session.list(Compaction)
        assert not row.membership_complete
        assert await session.count(CompactionMembership) == 0
    with pytest.raises(ValueError, match="every compaction to be linked"):
        await app.conversations.effective_messages(conversation)


@pytest.mark.parametrize("retain_answer", [True, False])
async def test_injected_context_does_not_make_retained_answers_shadowed(
    in_memory_engine: AsyncEngine, retain_answer: bool
) -> None:
    app = Octomate()
    _, tailer = wired(app)
    records = turn_events(1, 0, "first", "one")
    records[2]["seq"] = 3
    records[3]["seq"] = 4
    records.insert(
        2,
        ev(
            2,
            "user/message",
            {
                "content": [{"type": "text", "text": "Current time"}],
                "source": {"kind": "time-context"},
            },
        ),
    )
    await stream_events(
        tailer,
        app.nerve,
        [
            *records,
            *compaction_events(shadowed=[1, 2] if retain_answer else [1, 2, 3]),
        ],
    )
    conversation = await native_conversation(app)
    assert len(await app.conversations.history(conversation.id)) == 2
    async with async_session() as session:
        [row] = await session.list(Compaction)
    assert row.membership_complete
    effective = await app.conversations.effective_messages(conversation)
    assert [message.message_text for message in effective] == [
        "<summary>Earlier work summarized</summary>",
        *(["one"] if retain_answer else []),
    ]


async def test_consecutive_prompts_keep_distinct_surface_identities(
    in_memory_engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    patch_gateway(monkeypatch)
    records = turn_events(1, 0, "first", "done")
    records[2]["seq"] = 10
    records[3]["seq"] = 11
    FakeDeepseekApi.reset(
        [
            *records[:2],
            user_message(2, "second"),
            *compaction_events(manual=False, shadowed=[1, 2]),
            *records[2:],
        ]
    )
    app = Octomate()
    thread = await app.threads.enter(KEY)
    agent = DeepseekTentacle(
        app, auth=app.auth, habitats=app.habitats, config=DeepseekConfig()
    )
    async with agent:
        conversation = await agent.resolve_conversation(
            thread.id, conversation_address=KEY
        )
        await agent.run("first", conversation_address=KEY, conversation=conversation)
    assert [
        message.message_text
        for message in await app.conversations.history(conversation.id)
    ] == ["first", "second", "done"]
    assert len(await app.conversations.effective_messages(conversation)) == 2


@pytest.mark.parametrize("same_session", [False, True])
async def test_child_compaction_starts_after_inherited_rewrite(
    in_memory_engine: AsyncEngine, same_session: bool
) -> None:
    app = Octomate()
    parent = await app.conversations.ensure(
        await a_thread("parent"), agent_tentacle_id="deepseek"
    )
    child = await app.conversations.ensure(
        await a_thread("child"), agent_tentacle_id="deepseek"
    )
    tracker = DeepseekCompactions(app.conversations, parent, "parent-session")
    accumulator = DeepseekRunAccumulator()
    for raw in turn_events(1, 0, "old", "old answer"):
        list(accumulator.consume(session_event("parent-session", raw)))
    await app.conversations.record_agent_run(
        parent, str(uuid7()), accumulator.messages, external_id="parent-session"
    )
    for raw in compaction_events():
        await tracker.consume(SessionEvent.model_validate(raw))
    await tracker.record_memberships()
    [inherited] = await app.conversations.effective_messages(parent)
    session_id = "parent-session" if same_session else "child-session"
    await app.conversations.fork(parent, child, external_id=session_id)
    accumulator = DeepseekRunAccumulator()
    start = 10 if same_session else 0
    for raw in turn_events(2, start, "new", "new answer"):
        list(accumulator.consume(session_event(session_id, raw)))
    await app.conversations.record_agent_run(
        child, str(uuid7()), accumulator.messages, external_id=session_id
    )
    tracker = DeepseekCompactions(app.conversations, child, session_id)
    for raw in compaction_events(base=start + 5, shadowed=[start + 1, start + 2]):
        await tracker.consume(SessionEvent.model_validate(raw))
    await tracker.record_memberships()
    effective = await app.conversations.effective_messages(child)
    assert len(effective) == 2
    assert effective[0].id == inherited.id
    assert effective[1].compaction_id != inherited.compaction_id
    assert [
        message.id for message in await app.conversations.effective_messages(parent)
    ] == [inherited.id]
    assert len(await app.conversations.history(child.id)) == 4
    async with async_session() as session:
        assert await session.count(Compaction) == 2
        assert all(row.membership_complete for row in await session.list(Compaction))
