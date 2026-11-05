"""Claude boundaries and transcript summaries share one durable identity."""

from datetime import UTC, datetime

import pytest
from arcanus.materia.sqlalchemy import selectinload
from sqlalchemy.ext.asyncio import AsyncEngine
from uuid_utils.compat import uuid7

from octomate import Octomate
from octomate.database import async_session
from octomate.schemas.compactions import Compaction, ExternalCompaction
from octomate.schemas.thread import CLAUDE_NATIVE_ID, ThreadKey
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
    tailer = ClaudeTranscriptTailer(app.conversations, app.threads)
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
            native_id=CLAUDE_NATIVE_ID,
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
        native_id=CLAUDE_NATIVE_ID,
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
        app.threads, ThreadKey(CLAUDE_NATIVE_ID, "thread", SESSION_ID)
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
    tailer = ClaudeTranscriptTailer(app.conversations, app.threads)
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
