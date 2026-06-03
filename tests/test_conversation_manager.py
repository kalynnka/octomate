"""Unit tests for ConversationManager against an in-memory SQLite."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import datetime, timezone

import pytest
from pydantic_ai.messages import (
    ModelRequest as RawModelRequest,
)
from pydantic_ai.messages import (
    ModelResponse as RawModelResponse,
)
from pydantic_ai.messages import TextPart, ToolCallPart, UserPromptPart
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    async_sessionmaker,
    create_async_engine,
)

import octomate.database as database
from octomate.managers import ConversationManager
from octomate.models import Base
from octomate.schemas.base import sqlalchemy_materia
from octomate.schemas.conversation import ConversationKey


@pytest.fixture(autouse=True)
async def _in_memory_engine(
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncIterator[AsyncEngine]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    maker = async_sessionmaker(
        engine, class_=database.AsyncSession, expire_on_commit=False
    )

    database.engine.cache_clear()
    database.session_maker.cache_clear()
    monkeypatch.setattr(database, "engine", lambda: engine)
    monkeypatch.setattr(database, "session_maker", lambda: maker)

    with sqlalchemy_materia:
        yield engine

    await engine.dispose()


def _key(chat_id: str = "alice") -> ConversationKey:
    return ConversationKey(
        channel_tentacle_id="dev_ui",
        chat_type="private",
        chat_id=chat_id,
        user_id="dev",
        thread_id="",
    )


async def test_ensure_is_idempotent() -> None:
    service = ConversationManager()
    a = await service.ensure(_key(), agent_tentacle_id="inkling")
    b = await service.ensure(_key(), agent_tentacle_id="inkling")
    assert a.id == b.id


async def test_ensure_loads_existing_conversation() -> None:
    first = ConversationManager()
    created = await first.ensure(_key(), agent_tentacle_id="inkling")

    fresh = ConversationManager()
    fetched = await fresh.ensure(_key(), agent_tentacle_id="inkling")
    assert fetched.id == created.id


async def test_ensure_sets_agent_at_creation_and_keeps_it() -> None:
    service = ConversationManager()
    created = await service.ensure(_key(), agent_tentacle_id="inkling")
    assert created.agent_tentacle_id == "inkling"

    # The owning agent is set once at creation; a later ensure with a different
    # agent does not change it.
    again = await ConversationManager().ensure(_key(), agent_tentacle_id="other")
    assert again.id == created.id
    assert again.agent_tentacle_id == "inkling"


async def test_record_run_creates_run_and_persists_messages() -> None:
    service = ConversationManager()
    conversation = await service.ensure(_key(), agent_tentacle_id="inkling")

    run_id = "run-1"
    raw = [
        RawModelRequest(
            parts=[UserPromptPart(content="hi")],
            run_id=run_id,
            timestamp=datetime.now(timezone.utc),
        ),
        RawModelResponse(
            parts=[TextPart(content="hello")],
            run_id=run_id,
            timestamp=datetime.now(timezone.utc),
            finish_reason="stop",
        ),
    ]
    await service.record_agent_run(conversation, run_id=run_id, messages=raw)

    fresh = ConversationManager()
    reloaded = await fresh.ensure(_key(), agent_tentacle_id="inkling")
    runs = list(reloaded.runs)
    assert len(runs) == 1
    assert runs[0].id == run_id
    assert runs[0].name is None
    listed = list(runs[0].messages)
    assert len(listed) == 2
    kinds = {type(m).__name__ for m in listed}
    assert kinds == {"ModelRequest", "ModelResponse"}
    assert len(list(reloaded.messages)) == 2


async def test_record_run_preserves_finish_reason() -> None:
    """The blessed ModelResponse round-trips pydantic-ai's finish_reason."""
    service = ConversationManager()
    conversation = await service.ensure(_key(), agent_tentacle_id="inkling")
    run_id = "run-fr"

    await service.record_agent_run(
        conversation,
        run_id=run_id,
        messages=[
            RawModelResponse(
                parts=[TextPart(content="halt")],
                run_id=run_id,
                timestamp=datetime.now(timezone.utc),
                finish_reason="tool_call",
            ),
        ],
    )

    fresh = ConversationManager()
    reloaded = await fresh.ensure(_key(), agent_tentacle_id="inkling")
    msgs = list(reloaded.messages)
    assert len(msgs) == 1
    assert msgs[0].finish_reason == "tool_call"


async def test_record_run_persists_name() -> None:
    service = ConversationManager()
    conversation = await service.ensure(_key(), agent_tentacle_id="inkling")

    await service.record_agent_run(
        conversation,
        run_id="run-named",
        name="triage",
        messages=[
            RawModelResponse(
                parts=[TextPart(content="route")],
                run_id="run-named",
                timestamp=datetime.now(timezone.utc),
                finish_reason="stop",
            ),
        ],
    )

    fresh = ConversationManager()
    reloaded = await fresh.ensure(_key(), agent_tentacle_id="inkling")
    runs = list(reloaded.runs)
    assert len(runs) == 1
    assert runs[0].name == "triage"


async def test_record_run_no_op_for_empty_list() -> None:
    service = ConversationManager()
    conversation = await service.ensure(_key(), agent_tentacle_id="inkling")
    await service.record_agent_run(conversation, run_id="empty", messages=[])
    fresh = ConversationManager()
    reloaded = await fresh.ensure(_key(), agent_tentacle_id="inkling")
    assert list(reloaded.runs) == []


async def test_record_run_refreshes_cached_history() -> None:
    service = ConversationManager()
    conversation = await service.ensure(_key(), agent_tentacle_id="inkling")
    assert list(conversation.messages) == []

    await service.record_agent_run(
        conversation,
        run_id="run-1",
        messages=[
            RawModelRequest(
                parts=[UserPromptPart(content="hi")],
                run_id="run-1",
                timestamp=datetime.now(timezone.utc),
            ),
            RawModelResponse(
                parts=[TextPart(content="hello")],
                run_id="run-1",
                timestamp=datetime.now(timezone.utc),
                finish_reason="stop",
            ),
        ],
    )

    # record_agent_run refreshes the cached conversation from the DB; a hot
    # ensure() (cache hit, no cold reload) reflects the new run.
    hot = await service.ensure(_key(), agent_tentacle_id="inkling")
    assert len(list(hot.messages)) == 2


async def test_drop_trailing_deferral_removes_from_cache_and_db() -> None:
    service = ConversationManager()
    conversation = await service.ensure(_key(), agent_tentacle_id="inkling")
    await service.record_agent_run(
        conversation,
        run_id="run-defer",
        messages=[
            RawModelRequest(
                parts=[UserPromptPart(content="do it")],
                run_id="run-defer",
                timestamp=datetime.now(timezone.utc),
            ),
            RawModelResponse(
                parts=[
                    ToolCallPart(
                        tool_name="ask_questions",
                        args={"questions": [{"question": "?"}]},
                        tool_call_id="call_1",
                    )
                ],
                run_id="run-defer",
                timestamp=datetime.now(timezone.utc),
            ),
        ],
    )

    # ensure() returns the conversation re-cached by record_agent_run's refresh.
    conversation = await service.ensure(_key(), agent_tentacle_id="inkling")
    dropped = await service.drop_trailing_deferral(conversation)
    assert dropped is not None

    # The deferral is gone from the cache (hot) and the DB (cold reload).
    hot = await service.ensure(_key(), agent_tentacle_id="inkling")
    assert [type(m).__name__ for m in hot.messages] == ["ModelRequest"]
    cold = await ConversationManager().ensure(_key(), agent_tentacle_id="inkling")
    assert [type(m).__name__ for m in cold.messages] == ["ModelRequest"]

    # The trailing message is now a request, not a deferral — nothing to drop.
    conversation = await service.ensure(_key(), agent_tentacle_id="inkling")
    assert await service.drop_trailing_deferral(conversation) is None
