from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

import pytest

from octomate.schemas.actions import AgentMessage
from octomate.schemas.conversation import ConversationKey, UserProfile
from octomate.schemas.events import MessageEvent
from octomate.schemas.segments import (
    AtData,
    AtSegment,
    ImageSegment,
    MessageSegment,
    ReplySegment,
    TextSegment,
)
from octomate.tentacles.channel.base import (
    ChannelTentacle,
    DownloadedImage,
    PlatformMessage,
)


@dataclass
class FakeOctomate:
    kicks: list[tuple[ConversationKey, list[MessageEvent], str | None]] = field(
        default_factory=list
    )

    async def kick(
        self,
        key: ConversationKey,
        contents: list[MessageEvent],
        *,
        agent_id: str | None = None,
    ) -> None:
        self.kicks.append((key, contents, agent_id))


@dataclass
class FakeInk:
    self_profile: UserProfile = field(
        default_factory=lambda: UserProfile(user_id="bot", name="Bot")
    )
    user_profiles: dict[str, UserProfile] = field(default_factory=dict)
    sent: list[tuple[str, str, list[PlatformMessage], str | None, bool]] = field(
        default_factory=list
    )

    def inspect(self) -> UserProfile:
        return self.self_profile

    async def get_user_profile(self, user_id: str) -> UserProfile:
        return self.user_profiles.get(
            user_id,
            UserProfile(user_id=user_id, name=f"user-{user_id}"),
        )

    async def upload_media(self, data: bytes) -> str | None:
        return None

    async def download_image(
        self,
        seg: ImageSegment,
        message_id: str,
    ) -> DownloadedImage | None:
        return None

    async def send_message(
        self,
        chat_id: str,
        chat_type: str,
        messages: list[PlatformMessage],
        reply_to: str | None = None,
        reply_in_thread: bool = False,
    ) -> str | None:
        self.sent.append((chat_id, chat_type, messages, reply_to, reply_in_thread))
        return f"sent-{len(self.sent)}"


@dataclass
class FakeChromo:
    sip_calls: list[Any] = field(default_factory=list)
    squirt_calls: list[tuple[list[MessageSegment], str | None]] = field(
        default_factory=list
    )

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

    async def squirt(
        self,
        segments: list[MessageSegment],
        *,
        reply_to: str | None = None,
    ) -> list[PlatformMessage]:
        self.squirt_calls.append((list(segments), reply_to))
        return [PlatformMessage(msg_type="text", content="".join(map(str, segments)))]


class FakeChannelTentacle(ChannelTentacle):
    sent: list[tuple[str, str, list[PlatformMessage], str | None, bool]]

    def __init__(
        self,
        id: str,
        octomate: FakeOctomate,
        ink: FakeInk,
        chromo: FakeChromo,
        *,
        mention_only: bool = True,
    ) -> None:
        super().__init__(
            id=id,
            octomate=octomate,
            ink=ink,
            chromo=chromo,
            agent_id="inkling",
            mention_only=mention_only,
        )
        self.sent = ink.sent


@pytest.fixture
def channel() -> FakeChannelTentacle:
    return FakeChannelTentacle(
        id="chan1",
        octomate=FakeOctomate(),
        ink=FakeInk(),
        chromo=FakeChromo(),
    )


async def test_ingest_dispatches_event_to_octomate(
    channel: FakeChannelTentacle,
) -> None:
    raw = {
        "message_id": "m42",
        "user_id": "alice",
        "chat_id": "lobby",
        "chat_type": "group",
        "segments": [
            AtSegment(data=AtData(user_id="bot")),
            TextSegment(data={"text": "hello"}),
        ],
    }

    await channel.ingest(raw)

    octomate = channel.octomate
    assert isinstance(octomate, FakeOctomate)
    assert len(octomate.kicks) == 1
    key, events, agent_id = octomate.kicks[0]

    assert key.channel_tentacle_id == "chan1"
    assert key.chat_id == "lobby"
    assert key.chat_type == "group"
    assert key.user_id == "alice"
    assert agent_id == "inkling"

    event = events[0]
    assert event.tentacle_id == "chan1"
    assert event.self_id == "bot"
    assert event.sender.user_id == "alice"


async def test_group_mention_filter_ignores_unmentioned_events() -> None:
    channel = FakeChannelTentacle(
        id="chan1",
        octomate=FakeOctomate(),
        ink=FakeInk(),
        chromo=FakeChromo(),
        mention_only=True,
    )
    await channel.ingest(
        {
            "message_id": "m42",
            "user_id": "alice",
            "chat_id": "lobby",
            "chat_type": "group",
            "segments": [TextSegment(data={"text": "hello"})],
        }
    )

    octomate = channel.octomate
    assert isinstance(octomate, FakeOctomate)
    assert octomate.kicks == []


async def test_twitch_encodes_and_sends_platform_messages(
    channel: FakeChannelTentacle,
) -> None:
    key = ConversationKey(
        channel_tentacle_id="chan1",
        chat_type="private",
        chat_id="alice",
        user_id="alice",
    )
    message = AgentMessage(segments=[TextSegment(data={"text": "hi alice"})])

    await channel.twitch(key, message)

    assert len(channel.sent) == 1
    chat_id, chat_type, messages, reply_to, _ = channel.sent[0]
    assert chat_id == "alice"
    assert chat_type == "private"
    assert messages[0].content == "hi alice"
    assert reply_to is None


async def test_twitch_preserves_segments_after_reply_marker(
    channel: FakeChannelTentacle,
) -> None:
    key = ConversationKey(
        channel_tentacle_id="chan1",
        chat_type="group",
        chat_id="lobby",
        user_id="alice",
    )
    message = AgentMessage(
        segments=[
            ReplySegment(data={"id": "m1"}),
            TextSegment(data={"text": "after reply"}),
        ]
    )

    await channel.twitch(key, message)

    assert len(channel.sent) == 1
    assert channel.sent[0][3] == "m1"
    assert channel.sent[0][2][0].content == "after reply"


async def test_wave_iterates_and_twitches_each_message(
    channel: FakeChannelTentacle,
) -> None:
    key = ConversationKey(
        channel_tentacle_id="chan1",
        chat_type="private",
        chat_id="alice",
        user_id="alice",
    )

    async def messages() -> AsyncIterator[AgentMessage]:
        for text in ("first", "second", "third"):
            yield AgentMessage(segments=[TextSegment(data={"text": text})])

    await channel.wave(key, messages())

    assert len(channel.sent) == 3
    contents = [call[2][0].content for call in channel.sent]
    assert contents == ["first", "second", "third"]
