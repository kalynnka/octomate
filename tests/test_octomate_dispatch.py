from __future__ import annotations

from collections.abc import AsyncGenerator, AsyncIterator, Sequence
from dataclasses import dataclass, field
from typing import cast

import pytest
from pydantic_ai import AgentEventStream, AgentRunResult, AgentRunResultEvent
from pydantic_ai.messages import AgentStreamEvent, ModelMessage, UserContent
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    async_sessionmaker,
    create_async_engine,
)

import octomate.database as database
from octomate import Octomate
from octomate.models import Base
from octomate.schemas.base import sqlalchemy_materia
from octomate.schemas.conversation import ConversationKey
from octomate.schemas.events import MessageEvent
from octomate.schemas.segments import TextSegment
from octomate.tentacles.agent.base import AgentTentacle
from octomate.tentacles.channel.base import ChannelTentacle


@pytest.fixture(autouse=True)
async def _in_memory_engine(
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncIterator[AsyncEngine]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    maker = async_sessionmaker(
        engine,
        class_=database.AsyncSession,
        expire_on_commit=False,
    )

    database.engine.cache_clear()
    database.session_maker.cache_clear()
    monkeypatch.setattr(database, "engine", lambda: engine)
    monkeypatch.setattr(database, "session_maker", lambda: maker)

    with sqlalchemy_materia:
        yield engine

    await engine.dispose()


@dataclass
class FakeAgent:
    id: str = "inkling"
    octomate: Octomate | None = None
    turns: list[
        tuple[str | Sequence[UserContent] | None, list[ModelMessage], ConversationKey]
    ] = field(default_factory=list)

    def run_stream_events(
        self,
        user_prompt: str | Sequence[UserContent] | None = None,
        *,
        conversation_key: ConversationKey,
        message_history: Sequence[ModelMessage] | None = None,
    ) -> AgentEventStream[str]:
        async def events() -> AsyncGenerator[
            AgentStreamEvent | AgentRunResultEvent[str],
            None,
        ]:
            self.turns.append(
                (user_prompt, list(message_history or []), conversation_key)
            )
            yield AgentRunResultEvent(AgentRunResult("handled"))

        return AgentEventStream(events())


@dataclass
class FakeChannel:
    id: str = "im"
    octomate: Octomate | None = None
    sent: list[tuple[ConversationKey, list[str], list[MessageEvent]]] = field(
        default_factory=list
    )

    async def activate(self) -> None:
        pass

    async def deactivate(self) -> None:
        pass

    async def respond(
        self,
        key: ConversationKey,
        events: AsyncIterator[AgentStreamEvent | AgentRunResultEvent[str]],
        *,
        source_events: list[MessageEvent] | None = None,
    ) -> None:
        outputs: list[str] = []
        async for event in events:
            if isinstance(event, AgentRunResultEvent):
                outputs.append(event.result.output)
        self.sent.append((key, outputs, list(source_events or [])))

    async def respond_text(
        self,
        key: ConversationKey,
        text: str,
        *,
        source_events: list[MessageEvent] | None = None,
    ) -> None:
        self.sent.append((key, [text], list(source_events or [])))


async def test_octomate_kick_dispatches_directly_to_registered_agent() -> None:
    octomate = Octomate()
    agent = FakeAgent()
    channel = FakeChannel()
    octomate.register_agent("inkling", cast(AgentTentacle, agent))
    octomate.connect_channel("im", cast(ChannelTentacle, channel))

    key = ConversationKey(
        channel_tentacle_id="im",
        chat_type="private",
        chat_id="alice",
        user_id="alice",
    )
    event = MessageEvent(
        tentacle_id="im",
        message_id="m1",
        chat_type="private",
        chat_id="alice",
        user_id="alice",
        segments=[TextSegment(data={"text": "hi"})],
    )

    await octomate.kick(key, [event], agent_id="inkling")

    assert len(agent.turns) == 1
    assert agent.turns[0][0] == str(event)
    assert agent.turns[0][2] == key
    assert len(channel.sent) == 1
    assert channel.sent[0][1] == ["handled"]
    assert channel.sent[0][2] == [event]
