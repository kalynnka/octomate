"""Unit tests for ChannelTentacle base — ingest, twitch, wave."""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from octomate.schemas.actions import AgentMessage
from octomate.schemas.events import MessageEvent
from octomate.schemas.segments import (
    ImageSegment,
    MessageSegment,
    TextSegment,
)
from octomate.schemas.session import SessionKey, UserProfile
from octomate.tentacles.channel.base import ChannelTentacle


@dataclass
class FakeOctopus:
    kicks: list[tuple[SessionKey, list[MessageEvent]]] = field(default_factory=list)

    async def kick(self, key: SessionKey, contents: list[MessageEvent]) -> None:
        self.kicks.append((key, contents))


@dataclass
class FakeInk:
    self_profile: UserProfile = field(
        default_factory=lambda: UserProfile(user_id="bot", name="Bot")
    )
    user_profiles: dict[str, UserProfile] = field(default_factory=dict)
    sent: list[tuple[str, str, list[MessageSegment], str | None, bool]] = field(
        default_factory=list
    )

    def inspect(self) -> UserProfile:
        return self.self_profile

    async def get_user_profile(self, user_id: str) -> UserProfile:
        return self.user_profiles.get(
            user_id, UserProfile(user_id=user_id, name=f"user-{user_id}")
        )

    async def upload_media(self, data: bytes) -> str | None:
        return None

    async def download_media(
        self, resource_id: str, **kwargs: Any
    ) -> tuple[bytes, str] | None:
        return None

    async def send_message(
        self,
        chat_id: str,
        chat_type: str,
        segments: list[MessageSegment],
        reply_to: str | None = None,
        reply_in_thread: bool = False,
    ) -> str | None:
        self.sent.append(
            (chat_id, chat_type, list(segments), reply_to, reply_in_thread)
        )
        return f"sent-{len(self.sent)}"


@dataclass
class FakeChromo:
    """Sips a dict into a MessageEvent."""

    sip_calls: list[Any] = field(default_factory=list)

    async def sip(self, raw: Any) -> MessageEvent | None:
        self.sip_calls.append(raw)
        if not isinstance(raw, dict):
            return None
        return MessageEvent(
            message_id=raw.get("message_id", "m1"),
            user_id=raw.get("user_id", "u1"),
            chat_id=raw.get("chat_id", "c1"),
            chat_type=raw.get("chat_type", "private"),
            segments=raw.get("segments", []),
        )


def _segment_text(seg: MessageSegment) -> str:
    if isinstance(seg, TextSegment):
        return seg.data["text"]
    return str(seg)


class FakeChannelTentacle(ChannelTentacle):
    """Test channel: stubs every abstract method."""

    def __init__(
        self, id: str, octopus: FakeOctopus, ink: FakeInk, chromo: FakeChromo
    ) -> None:
        self.ink = ink
        self.chromo = chromo
        super().__init__(id, octopus)
        self.received_batches: list[tuple[SessionKey, list[MessageEvent]]] = []

    async def __call__(self, key: SessionKey, contents: list[MessageEvent]) -> None:
        self.received_batches.append((key, contents))

    async def activate(self) -> None:
        pass

    async def deactivate(self) -> None:
        pass

    async def absorb(self, seg: ImageSegment, save_dir: Path, message_id: str) -> None:
        pass

    async def secrete(self, seg: ImageSegment) -> None:
        pass


@pytest.fixture
def channel() -> FakeChannelTentacle:
    return FakeChannelTentacle(
        id="chan1",
        octopus=FakeOctopus(),
        ink=FakeInk(),
        chromo=FakeChromo(),
    )


async def test_ingest_kicks_octopus_directly_with_single_event(
    channel: FakeChannelTentacle,
) -> None:
    raw = {
        "message_id": "m42",
        "user_id": "alice",
        "chat_id": "lobby",
        "chat_type": "group",
        "segments": [TextSegment(data={"text": "hello"})],
    }

    await channel.ingest(raw)

    octopus = channel.octopus
    assert isinstance(octopus, FakeOctopus)
    assert len(octopus.kicks) == 1
    key, batch = octopus.kicks[0]

    assert isinstance(key, SessionKey)
    assert key.channel_tentacle_id == "chan1"
    assert key.chat_id == "lobby"
    assert key.chat_type == "group"
    assert key.user_id == "alice"

    assert len(batch) == 1
    event = batch[0]
    assert event.tentacle_id == "chan1"
    assert event.self_id == "bot"
    assert event.sender.user_id == "alice"

    assert isinstance(channel.chromo, FakeChromo)
    assert channel.chromo.sip_calls == [raw]


async def test_twitch_passes_segments_directly_to_ink(
    channel: FakeChannelTentacle,
) -> None:
    key = SessionKey(
        channel_tentacle_id="chan1",
        chat_type="private",
        chat_id="alice",
        user_id="alice",
    )
    message = AgentMessage(segments=[TextSegment(data={"text": "hi alice"})])

    await channel.twitch(key, message)

    assert len(channel.ink.sent) == 1
    chat_id, chat_type, segments, reply_to, _ = channel.ink.sent[0]
    assert chat_id == "alice"
    assert chat_type == "private"
    assert len(segments) == 1
    assert _segment_text(segments[0]) == "hi alice"
    assert reply_to is None


async def test_wave_iterates_and_twitches_each_message(
    channel: FakeChannelTentacle,
) -> None:
    key = SessionKey(
        channel_tentacle_id="chan1",
        chat_type="private",
        chat_id="alice",
        user_id="alice",
    )

    async def messages() -> AsyncIterator[AgentMessage]:
        for text in ("first", "second", "third"):
            yield AgentMessage(segments=[TextSegment(data={"text": text})])

    await channel.wave(key, messages())

    assert len(channel.ink.sent) == 3
    contents = [_segment_text(call[2][0]) for call in channel.ink.sent]
    assert contents == ["first", "second", "third"]


async def test_channel_tentacle_cannot_be_instantiated_directly() -> None:
    with pytest.raises(TypeError):
        ChannelTentacle(id="x", octopus=FakeOctopus())  # type: ignore[abstract]
