"""Unit tests for SessionManager against an in-memory SQLite."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import datetime, timezone

import pytest
from pydantic_ai.messages import TextPart, UserPromptPart
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    async_sessionmaker,
    create_async_engine,
)

import octomate.database as database
from octomate.models import Base
from octomate.schemas.base import sqlalchemy_materia
from octomate.schemas.messages import ModelRequest, ModelResponse
from octomate.schemas.session import SessionKey
from octomate.managers import SessionManager


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


def _key(chat_id: str = "alice") -> SessionKey:
    return SessionKey(
        channel_tentacle_id="dev_ui",
        chat_type="private",
        chat_id=chat_id,
        user_id="dev",
        thread_id="",
    )


async def test_get_or_create_is_idempotent() -> None:
    service = SessionManager()
    a = await service.ensure(_key())
    b = await service.ensure(_key())
    assert a.id == b.id


async def test_get_or_create_returns_cached_session_on_second_hit() -> None:
    service = SessionManager()
    a = await service.ensure(_key())
    assert _key() in service.cache
    b = await service.ensure(_key())
    assert a is b  # same instance — pulled from cache


async def test_get_or_create_loads_from_db_when_cache_misses() -> None:
    first = SessionManager()
    created = await first.ensure(_key())

    fresh = SessionManager()
    assert _key() not in fresh.cache
    fetched = await fresh.ensure(_key())
    assert fetched.id == created.id
    assert _key() in fresh.cache


async def test_cache_evicts_oldest_when_capacity_exceeded() -> None:
    service = SessionManager(cache_size=2)
    await service.ensure(_key("a"))
    await service.ensure(_key("b"))
    await service.ensure(_key("c"))

    assert _key("a") not in service.cache
    assert _key("b") in service.cache
    assert _key("c") in service.cache


async def test_append_messages_persists_and_invalidates_cache() -> None:
    service = SessionManager()
    session = await service.ensure(_key())
    assert session.key in service.cache

    msgs = [
        ModelRequest(
            parts=[UserPromptPart(content="hi")],
            timestamp=datetime.now(timezone.utc),
        ),
        ModelResponse(
            parts=[TextPart(content="hello")],
            timestamp=datetime.now(timezone.utc),
        ),
    ]
    await service.append_messages(session, msgs)

    # Cache evicted so a fresh read pulls the messages from the DB
    assert session.key not in service.cache
    fresh = SessionManager()
    reloaded = await fresh.ensure(_key())
    listed = list(reloaded.messages)
    assert len(listed) == 2
    kinds = {type(m).__name__ for m in listed}
    assert kinds == {"ModelRequest", "ModelResponse"}


async def test_append_messages_no_op_for_empty_list() -> None:
    service = SessionManager()
    session = await service.ensure(_key())
    cached_before = session.key in service.cache
    await service.append_messages(session, [])
    # No DB roundtrip, no eviction
    assert (session.key in service.cache) is cached_before
