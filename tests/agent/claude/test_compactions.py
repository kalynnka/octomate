"""Claude boundaries and transcript summaries share one durable identity."""

from datetime import UTC, datetime

import pytest
from arcanus.materia.sqlalchemy import selectinload
from claude_agent_sdk import UserMessage
from sqlalchemy.ext.asyncio import AsyncEngine
from uuid_utils.compat import uuid7

from octomate import Octomate
from octomate.database import async_session
from octomate.schemas.compactions import Compaction, ExternalCompaction
from octomate.schemas.thread import ThreadKey
from octomate.tentacles.claude.adapter import ClaudeRunAccumulator
from octomate.tentacles.claude.tailer import ClaudeTranscriptTailer
from octomate.types.compactions import CompactionTrigger
from octomate.types.json import JsonObject
from tests.agent.claude.test_tailer import (
    AGENT_ID,
    SESSION_ID,
    TURN_ONE,
    TURN_TWO,
    prompt_record,
    runs_of,
    stream_in,
)
from tests.support.managers import a_loaded_thread, a_thread


@pytest.mark.parametrize("trigger", ["manual", "auto"])
@pytest.mark.parametrize(
    ("child", "driven_first"), [(False, False), (False, True), (True, False)]
)
async def test_compaction_summary_never_becomes_a_prompt_or_run(
    in_memory_engine: AsyncEngine,
    trigger: CompactionTrigger,
    driven_first: bool,
    child: bool,
) -> None:
    app = Octomate()
    tailer = ClaudeTranscriptTailer(
        app.conversations, app.threads, agent_tentacle_id="claude"
    )
    boundary_id = str(uuid7())
    boundary = prompt_record("compact", "", 5)
    boundary.pop("message")
    boundary.pop("promptSource")
    boundary.pop("promptId")
    boundary.update(
        type="system",
        subtype="compact_boundary",
        level="info",
        uuid=boundary_id,
        compactMetadata={"trigger": trigger, "preTokens": 80000},
        isSidechain=child,
    )
    summary = prompt_record("compact", "Earlier work summarized", 6)
    summary.pop("promptSource")
    summary.update(
        uuid=str(uuid7()),
        parentUuid=boundary_id,
        isCompactSummary=True,
        isVisibleInTranscriptOnly=True,
        isSidechain=child,
    )
    records: list[JsonObject] = [boundary, summary]
    owner_id = None
    if driven_first and not child:
        conversation = await app.conversations.ensure(
            await a_thread(), agent_tentacle_id="claude"
        )
        owner_id = conversation.id
        await app.conversations.set_external_id(conversation, SESSION_ID)
        await app.conversations.record_driven_compaction(
            conversation,
            native_session_id=SESSION_ID,
            native_compaction_id=boundary_id,
            trigger=trigger,
            happened_at=None,
        )
    if child:
        parent = await tailer.ensure_session(SESSION_ID)
        await app.conversations.set_external_id(parent, SESSION_ID)
    for _ in range(2):
        await stream_in(
            tailer,
            app.nerve,
            [] if child else [*TURN_ONE, *records, *TURN_TWO],
            sub=records if child else None,
        )
    async with async_session() as session:
        rows = await session.list(
            Compaction, options=[selectinload(Compaction["messages"])]
        )
    [row] = rows
    assert row.native_compaction_id == boundary_id
    assert row.trigger == trigger
    assert row.happened_at == datetime(2026, 7, 9, 10, 0, 5, tzinfo=UTC)
    if owner_id is not None:
        assert row.conversation_id == owner_id
    [message] = row.messages
    assert message.message_text == "Earlier work summarized"
    assert message.run_id is None
    assert message.compaction_id == row.id
    original_ids = row.id, message.id
    await app.conversations.record_driven_compaction(
        await app.conversations.get(row.conversation_id),
        native_session_id=SESSION_ID,
        native_compaction_id=boundary_id,
        trigger=trigger,
        happened_at=None,
    )
    async with async_session() as session:
        [replayed] = await session.list(
            Compaction, options=[selectinload(Compaction["messages"])]
        )
    assert (replayed.id, replayed.messages[0].id) == original_ids
    assert len(await runs_of(app)) == (0 if child else 2)
    thread = await a_loaded_thread(
        app.threads, ThreadKey("claude", "thread", SESSION_ID)
    )
    assert all(item.message_text != message.message_text for item in thread.messages)
    if child:
        conversations = await app.conversations.for_thread(thread.id)
        [owner] = [item for item in conversations if item.subagent_id == AGENT_ID]
        assert row.conversation_id == owner.id
    if isinstance(row, ExternalCompaction):
        assert row.source is not None
        assert row.start_offset is not None
        assert row.end_offset is not None
        assert row.end_offset > row.start_offset


async def test_boundary_without_summary_still_persists_without_a_run(
    in_memory_engine: AsyncEngine,
) -> None:
    app = Octomate()
    tailer = ClaudeTranscriptTailer(
        app.conversations, app.threads, agent_tentacle_id="claude"
    )
    boundary = prompt_record("compact", "", 1)
    boundary.update(
        type="system",
        subtype="compact_boundary",
        level="info",
        compactMetadata={"trigger": "manual"},
    )
    await stream_in(tailer, app.nerve, [boundary])
    async with async_session() as session:
        [row] = await session.list(
            Compaction, options=[selectinload(Compaction["messages"])]
        )
    assert row.trigger == "manual"
    assert not row.messages
    assert not await runs_of(app)


@pytest.mark.parametrize("retained", [[], ["u-p1", "a2"], ["missing"]])
@pytest.mark.parametrize("references", ["uuids", "allUuids"])
async def test_retained_transcript_uuids_preserve_existing_messages(
    in_memory_engine: AsyncEngine, retained: list[str], references: str
) -> None:
    app = Octomate()
    tailer = ClaudeTranscriptTailer(
        app.conversations, app.threads, agent_tentacle_id="claude"
    )
    boundary_id, summary_id = str(uuid7()), str(uuid7())
    boundary = prompt_record("compact", "", 5)
    boundary.pop("message")
    boundary.pop("promptSource")
    boundary.pop("promptId")
    boundary.update(
        type="system",
        subtype="compact_boundary",
        level="info",
        uuid=boundary_id,
        compactMetadata={
            "trigger": "manual",
            "preservedMessages": {
                "allUuids": ["unseen"],
                "anchorUuid": summary_id,
                references: [*retained, "stop-notice"],
            },
        },
    )
    summary = prompt_record("compact", "summary", 6)
    summary.pop("promptSource")
    summary.update(uuid=summary_id, parentUuid=boundary_id, isCompactSummary=True)
    notice = {**boundary, "uuid": "stop-notice", "subtype": "stop_hook_summary"}
    notice.pop("compactMetadata")
    records = [*TURN_ONE, notice, boundary, summary, *TURN_TWO]
    await stream_in(tailer, app.nerve, records)
    async with async_session() as session:
        [row] = await session.list(
            Compaction, options=[selectinload(Compaction["messages"])]
        )
    owner = await app.conversations.get(row.conversation_id)
    archive = await app.conversations.history(owner.id)
    assert len(archive) == 6
    assert row.membership_complete is (retained != ["missing"])
    if row.membership_complete:
        expected = [
            row.messages[0].id,
            *([archive[0].id, archive[3].id] if retained else []),
            archive[4].id,
            archive[5].id,
        ]
        assert [
            message.id for message in await app.conversations.effective_messages(owner)
        ] == expected
        child = await app.conversations.ensure(
            await a_thread("child"), agent_tentacle_id="claude"
        )
        await app.conversations.fork(owner, child, external_id="child-session")
        assert [
            message.id for message in await app.conversations.effective_messages(child)
        ] == expected
    await stream_in(tailer, app.nerve, records)
    assert [message.id for message in await app.conversations.history(owner.id)] == [
        message.id for message in archive
    ]


def test_sdk_summary_cannot_replace_the_prompt_identity() -> None:
    accumulator = ClaudeRunAccumulator()
    accumulator.begin("original")
    list(accumulator.consume(UserMessage(content="summary", uuid="summary")))
    assert accumulator.messages[0].metadata is None
    list(accumulator.consume(UserMessage(content="original", uuid="prompt")))
    list(accumulator.consume(UserMessage(content="original", uuid="later")))
    assert accumulator.messages[0].metadata == {"native_item_ids": ["prompt"]}
