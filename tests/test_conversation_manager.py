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
from pydantic_ai.messages import TextPart, UserPromptPart
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
    a = await service.ensure(_key())
    b = await service.ensure(_key())
    assert a.id == b.id


async def test_ensure_loads_existing_conversation() -> None:
    first = ConversationManager()
    created = await first.ensure(_key())

    fresh = ConversationManager()
    fetched = await fresh.ensure(_key())
    assert fetched.id == created.id


async def test_record_run_creates_run_and_persists_messages() -> None:
    service = ConversationManager()
    conversation = await service.ensure(_key())

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
    reloaded = await fresh.ensure(_key())
    runs = list(reloaded.runs)
    assert len(runs) == 1
    assert runs[0].id == run_id
    listed = list(runs[0].messages)
    assert len(listed) == 2
    kinds = {type(m).__name__ for m in listed}
    assert kinds == {"ModelRequest", "ModelResponse"}
    assert len(list(reloaded.messages)) == 2


async def test_record_run_preserves_finish_reason() -> None:
    """The blessed ModelResponse round-trips pydantic-ai's finish_reason."""
    service = ConversationManager()
    conversation = await service.ensure(_key())
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
    reloaded = await fresh.ensure(_key())
    msgs = list(reloaded.messages)
    assert len(msgs) == 1
    assert msgs[0].finish_reason == "tool_call"


async def test_record_run_no_op_for_empty_list() -> None:
    service = ConversationManager()
    conversation = await service.ensure(_key())
    await service.record_agent_run(conversation, run_id="empty", messages=[])
    fresh = ConversationManager()
    reloaded = await fresh.ensure(_key())
    assert list(reloaded.runs) == []
