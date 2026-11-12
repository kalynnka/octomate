"""dsh completion records survive every producer and never invent a turn."""

import asyncio
from pathlib import Path

import pytest
from arcanus.materia.sqlalchemy import selectinload
from pydantic_ai.exceptions import AgentRunError
from sqlalchemy.ext.asyncio import AsyncEngine
from uuid_utils.compat import uuid7

from octomate import Octomate
from octomate.config.agents import DeepseekConfig
from octomate.database import async_session
from octomate.managers.conversation import ConversationManager
from octomate.schemas.commands import CommandContext, CommandResult
from octomate.schemas.compactions import Compaction
from octomate.tentacles.deepseek import DeepseekTentacle
from octomate.tentacles.deepseek.compactions import DeepseekCompactions
from octomate.tentacles.deepseek.ink import TurnFrame
from octomate.tentacles.deepseek.wire import CommandExecutionValue, SessionEvent
from octomate.types.json import JsonObject
from tests.agent.deepseek.test_native_ingest import (
    SESSION_ID,
    ev,
    native_conversation,
    stream_events,
    turn_events,
    wired,
)
from tests.agent.deepseek.test_tentacle import (
    KEY,
    FakeDeepseekApi,
    patch_gateway,
    session_event,
)


def compaction_events(*, manual: bool = True, failed: bool = False) -> list[JsonObject]:
    lifecycle: JsonObject = {
        "compactionId": str(uuid7()),
        "turn": None if manual else 1,
    }
    if manual:
        lifecycle["sourceCommandId"] = "cmd-1"
    return [
        ev(5, "compaction/start", lifecycle),
        ev(
            6,
            "compaction/summary",
            {
                **lifecycle,
                "summary": [{"type": "text", "text": "Earlier work summarized"}],
                "provider": "deepseek",
                "model": "deepseek-v4-flash",
                "shadowedRange": {"start": 1, "end": 3},
                "shadowedSeqs": [1, 2, 3],
                "shadowedTokenCount": 10000,
            },
        ),
        ev(
            7,
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
            8,
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


async def test_driven_command_compaction_has_summary_without_a_model_run(
    in_memory_engine: AsyncEngine,
) -> None:
    app = Octomate()
    thread = await app.threads.enter(KEY)
    conversation = await app.conversations.ensure(
        thread.id, agent_tentacle_id="deepseek"
    )
    await app.conversations.set_external_id(conversation, "sess-1")
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
    async with async_session() as session:
        [row] = await session.list(
            Compaction, options=[selectinload(Compaction["messages"])]
        )
    assert row.kind == "octomate"
    assert row.messages[0].message_text == "<summary>Earlier work summarized</summary>"
    assert not (await app.conversations.get(conversation.id)).runs


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
    FakeDeepseekApi.reset(
        [*records[:2], *compaction_events(manual=False), *records[2:]]
    )
    app = Octomate()
    thread = await app.threads.enter(KEY)
    agent = DeepseekTentacle(
        app, auth=app.auth, habitats=app.habitats, config=DeepseekConfig()
    )
    async with agent:
        if failed_run:
            with pytest.raises(AgentRunError):
                await agent.run("work", conversation_address=KEY, thread_id=thread.id)
        else:
            await agent.run("work", conversation_address=KEY, thread_id=thread.id)
    async with async_session() as session:
        [row] = await session.list(
            Compaction, options=[selectinload(Compaction["messages"])]
        )
    assert row.trigger == "auto"
    assert row.messages[0].message_text == "<summary>Earlier work summarized</summary>"
