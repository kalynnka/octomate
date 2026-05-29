from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import cast

import pytest
from pydantic_ai import AgentRunResult, AgentRunResultEvent
from pydantic_ai.messages import AgentStreamEvent

from octomate import Octomate
from octomate.config import ChannelConfig
from octomate.schemas.conversation import ConversationKey, UserProfile
from octomate.schemas.events import MessageEvent
from octomate.schemas.segments import (
    AtData,
    AtSegment,
    ImageSegment,
    MessageSegment,
    TextSegment,
)
from octomate.tentacles.channel.base import (
    ChannelTentacle,
    Chromo,
    DownloadedImage,
    Ink,
)
from octomate.tentacles.channel.stream import (
    StreamBlock,
    TextStreamBatcher,
)

NativeMessage = dict[str, str]
ChannelEvent = AgentStreamEvent | AgentRunResultEvent[str]


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
    sent: list[tuple[str, str, list[NativeMessage], str | None, bool]] = field(
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
        messages: list[NativeMessage],
        reply_to: str | None = None,
        reply_in_thread: bool = False,
    ) -> str | None:
        self.sent.append((chat_id, chat_type, messages, reply_to, reply_in_thread))
        return f"sent-{len(self.sent)}"


@dataclass
class FakeChromo:
    sip_calls: list[object] = field(default_factory=list)
    squirt_calls: list[str | None] = field(default_factory=list)

    async def sip(self, raw: object) -> MessageEvent | None:
        self.sip_calls.append(raw)
        if not isinstance(raw, dict):
            return None
        data = cast(dict[str, object], raw)
        chat_type = data.get("chat_type", "private")
        if chat_type not in ("private", "group"):
            chat_type = "private"
        return MessageEvent(
            message_id=str(data.get("message_id", "m1")),
            user_id=str(data.get("user_id", "u1")),
            chat_id=str(data.get("chat_id", "c1")),
            chat_type=chat_type,
            segments=cast(list[MessageSegment], data.get("segments", [])),
        )

    def squirt(
        self,
        result: AgentRunResult[str],
        *,
        reply_to: str | None = None,
    ) -> list[NativeMessage]:
        self.squirt_calls.append(reply_to)
        return [{"text": str(result.output)}]


class FakeChannelTentacle(ChannelTentacle):
    sent: list[tuple[str, str, list[NativeMessage], str | None, bool]]

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
            octomate=cast(Octomate, octomate),
            ink=cast(Ink, ink),
            chromo=cast(Chromo, chromo),
            config=ChannelConfig(
                type="fake",
                agent_id="inkling",
                mention_only=mention_only,
            ),
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


async def test_respond_encodes_final_agent_result_and_sends_native_messages(
    channel: FakeChannelTentacle,
) -> None:
    key = ConversationKey(
        channel_tentacle_id="chan1",
        chat_type="private",
        chat_id="alice",
        user_id="alice",
    )

    await channel.respond(key, AgentRunResult("hi alice"))

    assert len(channel.sent) == 1
    chat_id, chat_type, messages, reply_to, _ = channel.sent[0]
    assert chat_id == "alice"
    assert chat_type == "private"
    assert messages[0]["text"] == "hi alice"
    assert reply_to is None


async def test_respond_uses_conversation_thread_as_reply_target(
    channel: FakeChannelTentacle,
) -> None:
    key = ConversationKey(
        channel_tentacle_id="chan1",
        chat_type="group",
        chat_id="lobby",
        user_id="alice",
        thread_id="m1",
    )

    await channel.respond(key, AgentRunResult("after reply"))

    assert len(channel.sent) == 1
    assert channel.sent[0][3] == "m1"
    assert channel.sent[0][2][0]["text"] == "after reply"


async def test_stream_respond_default_warns_and_falls_back_to_final_response(
    channel: FakeChannelTentacle,
    caplog: pytest.LogCaptureFixture,
) -> None:
    key = ConversationKey(
        channel_tentacle_id="chan1",
        chat_type="private",
        chat_id="alice",
        user_id="alice",
    )

    async def events() -> AsyncIterator[ChannelEvent]:
        yield AgentRunResultEvent(AgentRunResult("stream me"))

    with caplog.at_level("WARNING"):
        await channel.stream_respond(key, events())

    assert "does not support streaming responses" in caplog.text
    assert len(channel.sent) == 1
    assert channel.sent[0][2][0]["text"] == "stream me"


async def test_respond_text_uses_normal_adapter(
    channel: FakeChannelTentacle,
) -> None:
    key = ConversationKey(
        channel_tentacle_id="chan1",
        chat_type="private",
        chat_id="alice",
        user_id="alice",
    )

    await channel.respond_text(key, "fallback")

    assert len(channel.sent) == 1
    assert channel.sent[0][2][0]["text"] == "fallback"


async def test_start_sub_thread_falls_back_to_main_target(
    channel: FakeChannelTentacle,
    caplog: pytest.LogCaptureFixture,
) -> None:
    key = ConversationKey(
        channel_tentacle_id="chan1",
        chat_type="private",
        chat_id="alice",
        user_id="alice",
    )

    with caplog.at_level("WARNING"):
        returned = await channel.start_sub_thread(key, "handoff")

    assert returned == key
    assert "does not support sub-thread startup" in caplog.text
    assert len(channel.sent) == 1
    assert channel.sent[0][2][0]["text"] == "handoff"


def test_text_stream_batcher_immediate_mode_flushes_each_push() -> None:
    batcher = TextStreamBatcher(flush_interval=0)

    first = batcher.push_text("hel")
    second = batcher.push_text("lo")

    assert [update.delta_text for update in first + second] == ["hel", "lo"]
    assert [update.sequence for update in first + second] == [1, 2]


def test_text_stream_batcher_flushes_by_time_and_size() -> None:
    now = 0.0

    def clock() -> float:
        return now

    batcher = TextStreamBatcher(
        flush_interval=0.2,
        min_chars=3,
        max_chars=10,
        clock=clock,
    )

    assert batcher.push_text("he") == []
    now = 0.3
    updates = batcher.push_text("y")

    assert len(updates) == 1
    assert updates[0].delta_text == "hey"
    assert updates[0].full_text == "hey"

    updates = batcher.push_text("x" * 10)

    assert len(updates) == 1
    assert updates[0].delta_text == "x" * 10
    assert updates[0].full_text == "hey" + ("x" * 10)


def test_text_stream_batcher_final_flush_and_block_switching() -> None:
    batcher = TextStreamBatcher(flush_interval=999, min_chars=100)

    assert batcher.push_text("answer") == []
    updates = batcher.push_text(
        "thinking",
        block=StreamBlock(id="think-1", type="thinking", title="Thinking"),
    )

    assert len(updates) == 1
    assert updates[0].block_id == "answer"
    assert updates[0].delta_text == "answer"

    final_updates = batcher.finish_all()

    assert len(final_updates) == 1
    assert final_updates[0].block_id == "think-1"
    assert final_updates[0].delta_text == "thinking"
    assert final_updates[0].is_final is True


def test_text_stream_batcher_marks_large_blocks_foldable() -> None:
    batcher = TextStreamBatcher(flush_interval=0, fold_threshold=5)

    updates = batcher.push_text("abcdef")

    assert len(updates) == 1
    assert updates[0].foldable is True
