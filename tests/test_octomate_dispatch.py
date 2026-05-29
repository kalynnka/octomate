from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any, cast

import pytest
from pydantic_ai import AgentRunResult, AgentRunResultEvent, AgentStreamEvent
from pydantic_ai.messages import ModelMessage, UserContent
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    async_sessionmaker,
    create_async_engine,
)

import octomate.database as database
from octomate import Octomate
from octomate.config import ChannelConfig, ChannelStreamConfig
from octomate.models import Base
from octomate.schemas.base import sqlalchemy_materia
from octomate.schemas.conversation import ConversationKey
from octomate.schemas.events import MessageEvent
from octomate.schemas.segments import TextSegment
from octomate.tentacles.agent.graph import TriageDecision
from octomate.tentacles.agent.base import AgentTentacle
from octomate.tentacles.channel.base import ChannelTentacle, ThreadStrategy


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
    triage_decision: TriageDecision = field(
        default_factory=lambda: TriageDecision(action="answer", answer="handled")
    )
    reception_output: str = "handled"
    turns: list[
        tuple[
            str | Sequence[UserContent] | None,
            list[ModelMessage],
            ConversationKey,
            str | None,
        ]
    ] = field(default_factory=list)
    streams: list[
        tuple[
            str | Sequence[UserContent] | None,
            list[ModelMessage],
            ConversationKey,
            str | None,
        ]
    ] = field(default_factory=list)

    async def run(
        self,
        user_prompt: str | Sequence[UserContent] | None = None,
        *,
        conversation_key: ConversationKey,
        run_name: str | None = None,
        message_history: Sequence[ModelMessage] | None = None,
        **kwargs: Any,
    ) -> AgentRunResult[Any]:
        self.turns.append(
            (user_prompt, list(message_history or []), conversation_key, run_name)
        )
        if run_name == "reception":
            raise AssertionError("reception should use run_stream_events")
        return AgentRunResult(self.triage_decision)

    @asynccontextmanager
    async def run_stream_events(
        self,
        user_prompt: str | Sequence[UserContent] | None = None,
        *,
        conversation_key: ConversationKey,
        run_name: str | None = None,
        message_history: Sequence[ModelMessage] | None = None,
        **kwargs: Any,
    ) -> AsyncIterator[AsyncIterator[AgentStreamEvent | AgentRunResultEvent[Any]]]:
        self.streams.append(
            (user_prompt, list(message_history or []), conversation_key, run_name)
        )

        async def events() -> AsyncIterator[
            AgentStreamEvent | AgentRunResultEvent[Any]
        ]:
            yield AgentRunResultEvent(AgentRunResult(self.reception_output))

        yield events()


@dataclass
class FakeChannel:
    id: str = "im"
    octomate: Octomate | None = None
    thread_strategy: ThreadStrategy = "flat_thread"
    config: ChannelConfig = field(
        default_factory=lambda: ChannelConfig(type="fake")
    )
    sent: list[tuple[ConversationKey, list[str]]] = field(default_factory=list)
    stream_sent: list[tuple[ConversationKey, list[str]]] = field(default_factory=list)
    sub_threads: list[tuple[ConversationKey, str]] = field(default_factory=list)

    async def activate(self) -> None:
        pass

    async def deactivate(self) -> None:
        pass

    async def respond(
        self,
        key: ConversationKey,
        result: AgentRunResult[str],
    ) -> None:
        self.sent.append((key, [result.output]))

    async def stream_respond(
        self,
        key: ConversationKey,
        events: AsyncIterator[AgentStreamEvent | AgentRunResultEvent[str]],
    ) -> None:
        outputs: list[str] = []
        async for event in events:
            if isinstance(event, AgentRunResultEvent):
                outputs.append(event.result.output)
        self.stream_sent.append((key, outputs))

    async def respond_text(
        self,
        key: ConversationKey,
        text: str,
    ) -> None:
        self.sent.append((key, [text]))

    async def start_sub_thread(
        self,
        key: ConversationKey,
        hint_text: str,
    ) -> ConversationKey:
        self.sub_threads.append((key, hint_text))
        return ConversationKey(
            channel_tentacle_id=key.channel_tentacle_id,
            chat_type=key.chat_type,
            chat_id=key.chat_id,
            user_id=key.user_id,
            thread_id="hint-thread",
        )


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
    assert agent.turns[0][3] == "triage"
    assert len(channel.sent) == 1
    assert channel.sent[0][1] == ["handled"]
    assert channel.stream_sent == []
    assert agent.streams == []


async def test_octomate_kick_streams_reception_result_when_enabled() -> None:
    octomate = Octomate()
    agent = FakeAgent(
        triage_decision=TriageDecision(
            action="reception",
            target_id="im",
            reason="debugging",
            handoff="Please continue debugging in reception.",
        )
    )
    channel = FakeChannel(
        config=ChannelConfig(
            type="fake",
            stream=ChannelStreamConfig(enabled=True),
        ),
    )
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

    assert channel.sent == []
    assert len(channel.sub_threads) == 1
    assert channel.sub_threads[0][1] == "debugging"
    assert len(channel.stream_sent) == 1
    assert channel.stream_sent[0][0].thread_id == "hint-thread"
    assert channel.stream_sent[0][1] == ["handled"]
    assert len(agent.turns) == 1
    assert len(agent.streams) == 1
    assert agent.turns[0][3] == "triage"
    assert agent.streams[0][0] == "Please continue debugging in reception."
    assert agent.streams[0][1] == []
    assert agent.streams[0][2].thread_id == "hint-thread"
    assert agent.streams[0][3] == "reception"


async def test_octomate_kick_skips_triage_inside_flat_thread() -> None:
    octomate = Octomate()
    agent = FakeAgent()
    channel = FakeChannel(
        config=ChannelConfig(
            type="fake",
            stream=ChannelStreamConfig(enabled=True),
        ),
    )
    octomate.register_agent("inkling", cast(AgentTentacle, agent))
    octomate.connect_channel("im", cast(ChannelTentacle, channel))

    key = ConversationKey(
        channel_tentacle_id="im",
        chat_type="private",
        chat_id="alice",
        user_id="alice",
        thread_id="existing-thread",
    )
    event = MessageEvent(
        tentacle_id="im",
        message_id="m1",
        chat_type="private",
        chat_id="alice",
        user_id="alice",
        thread_id="existing-thread",
        segments=[TextSegment(data={"text": "continue"})],
    )

    await octomate.kick(key, [event], agent_id="inkling")

    assert agent.turns == []
    assert len(agent.streams) == 1
    assert agent.streams[0][2] == key
    assert agent.streams[0][3] == "reception"
    assert channel.sub_threads == []
    assert channel.stream_sent[0][0] == key
    assert channel.stream_sent[0][1] == ["handled"]


async def test_octomate_kick_routes_reception_to_attached_channel_sub_thread() -> None:
    octomate = Octomate()
    agent = FakeAgent(
        triage_decision=TriageDecision(
            action="reception",
            target_id="ops",
            reason="needs work",
            hint="Working on it",
            handoff="Please investigate this in ops.",
        )
    )
    source = FakeChannel()
    ops = FakeChannel(
        id="ops",
        config=ChannelConfig(
            type="fake",
            stream=ChannelStreamConfig(enabled=True),
        ),
    )
    octomate.register_agent("inkling", cast(AgentTentacle, agent))
    octomate.connect_channel("im", cast(ChannelTentacle, source))
    octomate.connect_channel("ops", cast(ChannelTentacle, ops))

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
        segments=[TextSegment(data={"text": "please investigate"})],
    )

    await octomate.kick(key, [event], agent_id="inkling")

    assert source.sent == []
    assert len(ops.sub_threads) == 1
    assert ops.sub_threads[0][1] == "Working on it"
    assert ops.sent == []
    assert len(ops.stream_sent) == 1
    target_key, outputs = ops.stream_sent[0]
    assert target_key.channel_tentacle_id == "ops"
    assert target_key.chat_id == "alice"
    assert target_key.thread_id == "hint-thread"
    assert outputs == ["handled"]
    assert len(agent.turns) == 1
    assert len(agent.streams) == 1
    assert agent.turns[0][3] == "triage"
    assert agent.streams[0][0] == "Please investigate this in ops."
    assert agent.streams[0][1] == []
    assert agent.streams[0][2] == target_key
    assert agent.streams[0][3] == "reception"


async def test_octomate_kick_keeps_reception_in_main_for_main_only_channel() -> None:
    octomate = Octomate()
    agent = FakeAgent(
        triage_decision=TriageDecision(
            action="reception",
            target_id="im",
            reason="needs work",
            handoff="Please investigate this in main.",
        )
    )
    channel = FakeChannel(
        thread_strategy="main_only",
        config=ChannelConfig(
            type="fake",
            stream=ChannelStreamConfig(enabled=True),
        ),
    )
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
        segments=[TextSegment(data={"text": "please investigate"})],
    )

    await octomate.kick(key, [event], agent_id="inkling")

    assert channel.sub_threads == []
    assert channel.sent == []
    assert len(channel.stream_sent) == 1
    assert channel.stream_sent[0][0] == key
    assert agent.turns[0][3] == "triage"
    assert agent.streams[0][0] == "Please investigate this in main."
    assert agent.streams[0][2] == key
    assert agent.streams[0][3] == "reception"
