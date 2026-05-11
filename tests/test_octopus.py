"""End-to-end orchestration tests: kick → agent, emit → channel, cross-channel."""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path

import anyio
import pytest
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    async_sessionmaker,
    create_async_engine,
)

import octomate.database as database
from octomate.models import Base
from octomate.nerve import SendSegments, StreamFrame, StreamSink
from octomate.octopus import Octomate
from octomate.schemas.actions import AgentMessage
from octomate.schemas.base import sqlalchemy_materia
from octomate.schemas.conversation import Conversation, ConversationKey, UserProfile
from octomate.schemas.events import AgentInput, MessageEvent
from octomate.schemas.segments import ImageSegment, MessageSegment, TextSegment
from octomate.tentacles.agent.base import AgentTentacle
from octomate.tentacles.channel.base import ChannelTentacle


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
    with sqlalchemy_materia:
        yield engine
    await engine.dispose()


@dataclass
class _StubInk:
    def inspect(self) -> UserProfile:
        return UserProfile(user_id="bot", name="Bot")

    async def get_user_profile(self, user_id: str) -> UserProfile:
        return UserProfile(user_id=user_id, name=user_id)

    async def upload_media(self, data: bytes) -> str | None:
        return None

    async def download_media(self, resource_id: str, **kwargs) -> None:
        return None

    async def send_message(self, *args, **kwargs) -> None:
        return None


@dataclass
class _StubChromo:
    async def sip(self, raw) -> MessageEvent | None:
        return None


class _FakeSink:
    def __init__(self) -> None:
        self.frames: list[StreamFrame] = []
        self.closed = False

    async def write(self, frame: StreamFrame) -> None:
        self.frames.append(frame)

    async def aclose(self) -> None:
        self.closed = True


class _RecordingChannel(ChannelTentacle):
    """Records `twitch` calls and exposes its sink for assertions."""

    def __init__(self, id: str) -> None:
        self.ink = _StubInk()
        self.chromo = _StubChromo()
        super().__init__(id)
        self.twitched: list[tuple[ConversationKey, AgentMessage]] = []
        self.sink = _FakeSink()

    async def twitch(self, key: ConversationKey, message: AgentMessage) -> None:
        self.twitched.append((key, message))

    def open_stream(self, conversation: Conversation) -> StreamSink:
        return self.sink

    async def absorb(self, seg: ImageSegment, save_dir: Path, message_id: str) -> None:
        return None

    async def secrete(self, seg: ImageSegment) -> None:
        return None


class _EchoAgent(AgentTentacle):
    """Emits one StreamFrame + one SendSegments per incoming user event."""

    def __init__(
        self,
        id: str,
        *,
        target_channel_for_segments: str | None = None,
    ) -> None:
        super().__init__(id)
        self.calls: list[tuple[Conversation, list[AgentInput]]] = []
        # If set, redirect SendSegments to a different conversation that lives
        # on this other channel. Used to exercise cross-channel routing.
        self._reroute_channel = target_channel_for_segments

    async def __call__(
        self, conversation: Conversation, events: list[AgentInput]
    ) -> None:
        self.calls.append((conversation, events))
        # Streamed bit
        await self.octopus.emit(
            StreamFrame(
                target_key=conversation.key,
                frame_type="append",
                payload={"echo": True},
            )
        )
        # Discrete reply — optionally routed cross-channel
        target_key = conversation.key
        if self._reroute_channel is not None:
            target_key = ConversationKey(
                channel_tentacle_id=self._reroute_channel,
                chat_type="private",
                chat_id="alice",
                user_id="dev",
                thread_id="",
            )
            # Ensure target conversation exists on the other channel.
            await self.octopus.conversations.ensure(
                target_key, agent_tentacle_id=self.id
            )
        await self.octopus.emit(
            SendSegments(
                target_key=target_key,
                segments=[TextSegment(data={"text": "pong"})],
            )
        )
        await self.octopus.emit(
            StreamFrame(target_key=conversation.key, frame_type="close")
        )


def _key(channel: str, chat: str = "alice") -> ConversationKey:
    return ConversationKey(
        channel_tentacle_id=channel,
        chat_type="private",
        chat_id=chat,
        user_id="dev",
        thread_id="",
    )


def _evt(text: str = "hi") -> MessageEvent:
    return MessageEvent(
        user_id="dev",
        chat_id="alice",
        chat_type="private",
        segments=[TextSegment(data={"text": text})],
    )


async def test_kick_routes_to_configured_agent() -> None:
    """`kick(key, events)` dispatches to the agent named on the conversation."""
    octopus = Octomate()
    agent = _EchoAgent("inkling")
    channel = _RecordingChannel("dev_ui")
    octopus.connect(agent)
    octopus.connect(channel)

    key = _key("dev_ui")
    await octopus.conversations.ensure(key, agent_tentacle_id="inkling")

    async with octopus.run():
        await octopus.kick(key, [_evt()])
        # Let the dispatchers drain.
        await anyio.sleep(0.1)

    assert len(agent.calls) == 1
    assert agent.calls[0][0].key == key


async def test_emit_send_segments_routes_to_channel() -> None:
    """`SendSegments` reaches the channel identified by `target_key`."""
    octopus = Octomate()
    agent = _EchoAgent("inkling")
    channel = _RecordingChannel("dev_ui")
    octopus.connect(agent)
    octopus.connect(channel)

    key = _key("dev_ui")
    await octopus.conversations.ensure(key, agent_tentacle_id="inkling")

    async with octopus.run():
        await octopus.kick(key, [_evt()])
        await anyio.sleep(0.1)

    assert len(channel.twitched) == 1
    twitched_key, msg = channel.twitched[0]
    assert twitched_key == key
    assert _text(msg.segments[0]) == "pong"


async def test_stream_frame_routes_to_channel_sink_and_close() -> None:
    """`StreamFrame`s flow into the channel's sink; `close` terminates it."""
    octopus = Octomate()
    agent = _EchoAgent("inkling")
    channel = _RecordingChannel("dev_ui")
    octopus.connect(agent)
    octopus.connect(channel)

    key = _key("dev_ui")
    await octopus.conversations.ensure(key, agent_tentacle_id="inkling")

    async with octopus.run():
        await octopus.kick(key, [_evt()])
        await anyio.sleep(0.1)

    # The echo agent emits one StreamFrame(append) + one StreamFrame(close).
    # The close frame triggers SinkRegistry.close() — the sink's aclose
    # ran but write() was called for the append frame.
    assert len(channel.sink.frames) >= 1
    assert channel.sink.frames[0].frame_type == "append"
    assert channel.sink.closed is True


async def test_cross_channel_routing() -> None:
    """An agent can emit a SendSegments whose `target_key` lives on a *different*
    channel from the one that originated the inbound `MessageBatch`."""
    octopus = Octomate()
    chan_in = _RecordingChannel("dev_ui")
    chan_out = _RecordingChannel("slack")
    agent = _EchoAgent("router", target_channel_for_segments="slack")
    octopus.connect(agent)
    octopus.connect(chan_in)
    octopus.connect(chan_out)

    in_key = _key("dev_ui")
    await octopus.conversations.ensure(in_key, agent_tentacle_id="router")

    async with octopus.run():
        await octopus.kick(in_key, [_evt()])
        await anyio.sleep(0.1)

    # The streamed event still lands on the source channel...
    assert len(chan_in.sink.frames) >= 1
    # ...but the discrete reply was routed to the OTHER channel.
    assert len(chan_in.twitched) == 0
    assert len(chan_out.twitched) == 1


def _text(seg: MessageSegment) -> str:
    data = getattr(seg, "data", {}) or {}
    return str(data.get("text", "")) if isinstance(data, dict) else str(seg)
