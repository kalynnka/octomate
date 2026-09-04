from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import AsyncGenerator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

import pytest

from octomate.capabilities.harness.events import (
    ResultSegmentEvent,
    ResultTextDeltaEvent,
    TodoCreatedEvent,
)
from octomate.config import DiscordStreamConfig
from octomate.schemas.conversation import ChannelAddress
from octomate.schemas.segments import (
    ImageData,
    ImageSegment,
    ReplySegment,
)
from octomate.schemas.todos import Todo
from octomate.tentacles.discord.chromo import DiscordChromo
from octomate.tentacles.discord.feelers.output import DiscordTimelineFeeler
from octomate.tentacles.discord.ink import DiscordInk
from octomate.tentacles.discord.schema import DiscordOutboundMessage
from tests.support.channels import FakeChannelTentacle, drive
from tests.support.scenarios import mid_run_notice, play, streamed_text


@dataclass(frozen=True)
class DiscordSend:
    chat_id: str
    chat_type: str
    messages: tuple[DiscordOutboundMessage, ...]
    channel_thread_id: str
    reply_to: str | None


@dataclass(frozen=True)
class DiscordEdit:
    channel_id: str
    message_id: str
    content: str
    mentioned_user_ids: tuple[str, ...]


class RecordingDiscordInk(DiscordInk):
    def __init__(self, *, fail_edits: bool = False) -> None:
        self.sends: list[DiscordSend] = []
        self.edits: list[DiscordEdit] = []
        self.typing_events: list[tuple[str, str]] = []
        self.fail_edits = fail_edits

    async def send_message(
        self,
        chat_id: str,
        chat_type: str,
        messages: list[DiscordOutboundMessage],
        *,
        channel_thread_id: str,
        reply_to: str | None = None,
        reply_in_thread: bool = False,
    ) -> str:
        assert reply_in_thread is False
        self.sends.append(
            DiscordSend(
                chat_id=chat_id,
                chat_type=chat_type,
                messages=tuple(messages),
                channel_thread_id=channel_thread_id,
                reply_to=reply_to,
            )
        )
        return str(800 + len(self.sends))

    async def edit_message(
        self,
        channel_id: str,
        message_id: str,
        content: str,
        *,
        mentioned_user_ids: tuple[str, ...] = (),
    ) -> str:
        self.edits.append(
            DiscordEdit(
                channel_id=channel_id,
                message_id=message_id,
                content=content,
                mentioned_user_ids=mentioned_user_ids,
            )
        )
        if self.fail_edits:
            raise RuntimeError("Discord edit failed")
        return message_id

    @asynccontextmanager
    async def typing(self, channel_id: str) -> AsyncGenerator[None]:
        self.typing_events.append(("enter", channel_id))
        try:
            yield
        finally:
            self.typing_events.append(("exit", channel_id))


class SlowFirstEditInk(RecordingDiscordInk):
    def __init__(self) -> None:
        super().__init__()
        self.edit_started = asyncio.Event()
        self.release_edit = asyncio.Event()
        self.active_edits = 0
        self.max_active_edits = 0

    async def edit_message(
        self,
        channel_id: str,
        message_id: str,
        content: str,
        *,
        mentioned_user_ids: tuple[str, ...] = (),
    ) -> str:
        self.active_edits += 1
        self.max_active_edits = max(self.max_active_edits, self.active_edits)
        try:
            if not self.edits:
                self.edit_started.set()
                await self.release_edit.wait()
            return await super().edit_message(
                channel_id,
                message_id,
                content,
                mentioned_user_ids=mentioned_user_ids,
            )
        finally:
            self.active_edits -= 1


def discord_address() -> ChannelAddress:
    return ChannelAddress(
        channel_tentacle_id="discord",
        chat_type="thread",
        chat_id="400",
        user_id="100",
        channel_thread_id="500",
        shared=True,
    )


def discord_channel(
    ink: RecordingDiscordInk,
    stream: DiscordStreamConfig,
    *,
    clock: Callable[[], float] = time.monotonic,
) -> FakeChannelTentacle:
    channel = FakeChannelTentacle(id="discord")
    channel.feelers.timeline = DiscordTimelineFeeler(
        ink=ink,
        chromo=DiscordChromo(),
        stream_config=stream,
        ask_questions=channel.feelers.ask_questions,
        approvals=channel.feelers.approvals,
        oauth=channel.feelers.oauth,
        deferred_actions=channel.octomate.deferred_actions,
        clock=clock,
    )
    return channel


async def test_disabled_stream_uses_typing_then_sends_the_final_answer() -> None:
    ink = RecordingDiscordInk()
    channel = discord_channel(ink, DiscordStreamConfig(enabled=False))
    text = "x" * 2001

    message_id = await drive(
        channel,
        discord_address(),
        play(streamed_text(text[:1000], text[1000:])),
    )

    assert message_id == "801"
    assert ink.typing_events == [("enter", "500"), ("exit", "500")]
    assert ink.edits == []
    assert [send.chat_id for send in ink.sends] == ["400", "400"]
    assert [send.chat_type for send in ink.sends] == ["thread", "thread"]
    assert [send.channel_thread_id for send in ink.sends] == ["500", "500"]
    assert [send.reply_to for send in ink.sends] == [None, None]
    assert [len(send.messages[0].content) for send in ink.sends] == [2000, 1]
    assert "".join(send.messages[0].content for send in ink.sends) == text


async def test_stream_lazily_starts_and_honors_the_edit_interval() -> None:
    now = 0.0
    ink = RecordingDiscordInk()
    channel = discord_channel(
        ink,
        DiscordStreamConfig(enabled=True, flush_interval=1.0, min_chars=1),
        clock=lambda: now,
    )

    async with channel.feelers.timeline.open(discord_address()) as state:
        await state.answer_start()
        assert ink.sends == []

        await state.answer_delta("a")
        assert ink.sends[0].messages == (DiscordOutboundMessage(content="…"),)
        assert ink.edits == []

        now = 1.1
        await state.answer_delta("b")
        await asyncio.sleep(0)
        assert [edit.content for edit in ink.edits] == ["ab"]

    assert state.message_id == "801"
    assert [edit.content for edit in ink.edits] == ["ab"]


async def test_stream_edits_are_single_flight_and_coalesce() -> None:
    ink = SlowFirstEditInk()
    channel = discord_channel(
        ink,
        DiscordStreamConfig(enabled=True, flush_interval=0, min_chars=1),
    )

    async with channel.feelers.timeline.open(discord_address()) as state:
        await state.answer_delta("a")
        await ink.edit_started.wait()
        await state.answer_delta("b")
        await state.answer_delta("c")
        assert ink.active_edits == 1
        assert ink.max_active_edits == 1
        ink.release_edit.set()

    assert ink.max_active_edits == 1
    assert [edit.content for edit in ink.edits] == ["a", "abc"]


async def test_stream_rolls_at_2000_chars_and_replies_only_once() -> None:
    ink = RecordingDiscordInk()
    channel = discord_channel(
        ink,
        DiscordStreamConfig(
            enabled=True,
            flush_interval=999,
            min_chars=10_000,
            max_chars=10_000,
        ),
    )
    text = "x" * 2500
    events = [
        ResultSegmentEvent(segment=ReplySegment(data={"id": "700"})),
        ResultTextDeltaEvent(delta=text),
    ]

    message_id = await drive(channel, discord_address(), play(events))

    assert message_id == "802"
    assert [send.channel_thread_id for send in ink.sends] == ["500", "500"]
    assert [send.reply_to for send in ink.sends] == ["700", None]
    assert [send.messages[0].content for send in ink.sends] == ["…", "…"]
    assert [edit.channel_id for edit in ink.edits] == ["500", "500"]
    assert [len(edit.content) for edit in ink.edits] == [2000, 500]
    assert "".join(edit.content for edit in ink.edits) == text


async def test_mid_run_notice_rotates_without_rendering_thinking_or_tools() -> None:
    ink = RecordingDiscordInk()
    channel = discord_channel(
        ink,
        DiscordStreamConfig(enabled=True, flush_interval=0, min_chars=1),
    )

    message_id = await drive(channel, discord_address(), play(mid_run_notice()))

    assert message_id == "802"
    assert len(ink.sends) == 2
    final_edits = [edit.content for edit in ink.edits if edit.message_id == "802"]
    notice_edits = [edit.content for edit in ink.edits if edit.message_id == "801"]
    assert notice_edits[-1] == "The docs don't cover this — I'm trying another way."
    assert final_edits[-1] == "Found it: pinning the dependency fixes the flake."
    assert ink.typing_events == [("enter", "500"), ("exit", "500")]


async def test_todo_and_native_segment_rotate_the_answer_messages(
    tmp_path: Path,
) -> None:
    ink = RecordingDiscordInk()
    channel = discord_channel(
        ink,
        DiscordStreamConfig(
            enabled=True,
            flush_interval=999,
            min_chars=10_000,
            max_chars=10_000,
        ),
    )
    image = tmp_path / "diagram.png"
    todo = Todo(conversation_id=uuid4(), ref="T1", content="Read the docs")
    events = [
        ResultTextDeltaEvent(delta="status"),
        TodoCreatedEvent(todo=todo),
        ResultSegmentEvent(segment=ImageSegment(data=ImageData(file=str(image)))),
        ResultTextDeltaEvent(delta="final"),
    ]

    message_id = await drive(channel, discord_address(), play(events))

    assert message_id == "803"
    assert len(ink.sends) == 3
    assert ink.sends[1].messages == (DiscordOutboundMessage(attachment_paths=(image,)),)
    assert [(edit.message_id, edit.content) for edit in ink.edits] == [
        ("801", "status"),
        ("803", "final"),
    ]


async def test_cancellation_settles_the_partial_answer_once() -> None:
    ink = RecordingDiscordInk()
    channel = discord_channel(
        ink,
        DiscordStreamConfig(
            enabled=True,
            flush_interval=999,
            min_chars=10_000,
            max_chars=10_000,
        ),
    )

    async def cancel_timeline() -> None:
        async with channel.feelers.timeline.open(discord_address()) as state:
            await state.answer_delta("partial")
            raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await cancel_timeline()

    assert len(ink.sends) == 1
    assert [edit.content for edit in ink.edits] == ["partial"]
    assert ink.typing_events == [("enter", "500"), ("exit", "500")]


async def test_failed_edit_is_logged_and_not_retried(
    caplog: pytest.LogCaptureFixture,
) -> None:
    ink = RecordingDiscordInk(fail_edits=True)
    channel = discord_channel(
        ink,
        DiscordStreamConfig(
            enabled=True,
            flush_interval=999,
            min_chars=10_000,
            max_chars=10_000,
        ),
    )

    with caplog.at_level(logging.WARNING):
        message_id = await drive(
            channel,
            discord_address(),
            play(streamed_text("broken")),
        )

    assert message_id == "801"
    assert len(ink.sends) == 1
    assert [edit.content for edit in ink.edits] == ["broken"]
    assert caplog.text.count("failed to edit message") == 1
