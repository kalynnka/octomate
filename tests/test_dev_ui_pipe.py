"""DevUI through the pipe.

End-to-end smoke test: POST /api/chat → octopus.kick → AgentTentacle
emits frames via octopus.emit → DevUI's pre-registered StreamSink encodes
Vercel chunks into the SSE response.

Uses a `_ScriptedAgent` instead of the real Inkling to keep the test
hermetic (no live LLM calls). The point is the *pipe*, not the agent.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import httpx
import pytest
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    async_sessionmaker,
    create_async_engine,
)

import octomate.database as database
from octomate.models import Base
from octomate.nerve import SendSegments, StreamFrame
from octomate.octopus import Octomate
from octomate.schemas.base import sqlalchemy_materia
from octomate.schemas.conversation import Conversation
from octomate.schemas.events import AgentInput
from octomate.schemas.segments import TextSegment
from octomate.tentacles.agent.base import AgentTentacle
from octomate.tentacles.channel.dev_ui.base import DevUITentacle


@pytest.fixture(autouse=True)
async def _in_memory_engine(monkeypatch) -> AsyncIterator[AsyncEngine]:
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
    yield engine
    await engine.dispose()


class _ScriptedAgent(AgentTentacle):
    """Emits one SendSegments + StreamFrame(close) per inbound batch."""

    def __init__(self, id, text: str = "hello back") -> None:
        super().__init__(id)
        self.text = text

    async def __call__(
        self, conversation: Conversation, events: list[AgentInput]
    ) -> None:
        await self.octopus.emit(
            SendSegments(
                target_key=conversation.key,
                segments=[TextSegment(data={"text": self.text})],
            )
        )
        await self.octopus.emit(
            StreamFrame(target_key=conversation.key, frame_type="close")
        )


async def test_post_api_chat_streams_through_the_pipe() -> None:
    """Build the FastAPI app, manually drive octopus.run() (skipping
    lifespan because httpx ASGITransport doesn't run it by default), and
    fire a POST /api/chat that should land back as Vercel SSE chunks.
    """
    from fastapi import FastAPI

    octopus = Octomate()
    octopus.connect(_ScriptedAgent("scripted"))
    octopus.connect(DevUITentacle("dev_ui", agent_id="scripted"))

    # Build a bare FastAPI without `octopus.app()`'s lifespan so we can
    # drive `octopus.run()` ourselves alongside the request.
    app = FastAPI()
    app.state.octopus = octopus
    for channel in octopus.channels.values():
        router = channel.router()
        if router is not None:
            app.include_router(router)

    # Activate materia at lifespan scope so dispatcher tasks see it (the
    # `octopus.app()` lifespan does this in production; this test bypasses
    # lifespan by driving `octopus.run()` directly).
    with sqlalchemy_materia():
        async with octopus.run():
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(
                transport=transport, base_url="http://test"
            ) as client:
                body = {
                    "id": "convo-1",
                    "messages": [
                        {
                            "id": "m-1",
                            "role": "user",
                            "parts": [{"type": "text", "text": "hi there"}],
                        }
                    ],
                    "trigger": "submit-message",
                }
                async with client.stream("POST", "/api/chat", json=body) as r:
                    assert r.status_code == 200
                    chunks: list[bytes] = []
                    async for chunk in r.aiter_bytes():
                        chunks.append(chunk)
                        # Cap the wait — if anything hangs, this bounds it.
                        if len(chunks) > 200:
                            break
                    text = b"".join(chunks).decode("utf-8")

    assert "hello back" in text
    assert "done" in text.lower() or "finish" in text.lower()
