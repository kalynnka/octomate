"""Status is a transient presentation, independent of timeline I/O and run ownership."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import replace
from itertools import count
from unittest.mock import AsyncMock

import pytest
from pydantic_ai.messages import (
    FunctionToolCallEvent,
    FunctionToolResultEvent,
    PartDeltaEvent,
    PartStartEvent,
    TextPart,
    ThinkingPart,
    ThinkingPartDelta,
    ToolCallPart,
    ToolReturnPart,
)

from octomate.events import (
    ActionBatchEvent,
    ReflexAsleepEvent,
    ReflexAwakeEvent,
    ReflexPhaseChangedEvent,
    RunActivityChangedEvent,
    RunEndedEvent,
    RunErrorEvent,
    RunStartedEvent,
    SubagentSettledEvent,
    WireEvent,
)
from octomate.nerve import ChannelTopic, Impulse
from octomate.schemas.conversation import ChannelAddress
from octomate.schemas.deferred import DeferredQuestion
from octomate.schemas.triage import GatewayAction
from octomate.tentacles.feelers import status
from octomate.tentacles.feelers.status import StatusFeeler, StatusNarrator
from tests.channels.test_siphon import a_channel, a_run, texts
from tests.support.nerve import drawing

ADDRESS = ChannelAddress("im", "thread", "C1", "U1", "thread-1")


class RecordingStatusFeeler(StatusFeeler):
    def __init__(self) -> None:
        super().__init__()
        self.presented: asyncio.Queue[tuple[ChannelAddress, str]] = asyncio.Queue()

    async def present(self, address: ChannelAddress, text: str) -> None:
        self.presented.put_nowait((address, text))

    async def next(self) -> str:
        _, text = await asyncio.wait_for(self.presented.get(), 1)
        return text


@pytest.fixture
async def advance_time(monkeypatch: pytest.MonkeyPatch) -> Callable[[float], None]:
    loop = asyncio.get_running_loop()
    original = loop.time
    offset = 0.0

    def now() -> float:
        return original() + offset

    def advance(seconds: float) -> None:
        nonlocal offset
        offset += seconds

    monkeypatch.setattr(loop, "time", now)
    return advance


async def test_activity_labels_tools_and_explicit_waits() -> None:
    feeler = RecordingStatusFeeler()
    events: list[tuple[WireEvent, str]] = [
        (ReflexAwakeEvent(), "Waking…"),
        (ReflexPhaseChangedEvent(phase="routing"), "Finding the right agent…"),
        (RunStartedEvent(address=ADDRESS), "Starting…"),
        (PartStartEvent(index=0, part=ThinkingPart(content="private")), "Thinking…"),
        (FunctionToolCallEvent(ToolCallPart("lookup", {}, "tool-1")), "Lookup…"),
        (
            ActionBatchEvent(batch_id="async", blocks_run=False),
            "Working · Input requested…",
        ),
        (ActionBatchEvent(batch_id="unknown"), "Working · Input requested…"),
        (
            ActionBatchEvent(batch_id="blocking", blocks_run=True),
            "Waiting for your input…",
        ),
        (RunActivityChangedEvent(activity="compacting"), "Compacting…"),
        (
            PartStartEvent(index=1, part=TextPart(content="answer")),
            "Writing…",
        ),
        (RunEndedEvent(), ""),
        (ReflexPhaseChangedEvent(phase="finalizing"), "Wrapping up…"),
        (ReflexAsleepEvent(), ""),
    ]
    try:
        previous = None
        for event, expected in events:
            feeler.feed(ADDRESS, event)
            if expected != previous:
                assert await feeler.next() == expected
            previous = expected
    finally:
        await feeler.close()
    assert not feeler.queues


async def test_one_tool_result_and_child_activity_do_not_replace_parent_status() -> (
    None
):
    feeler = RecordingStatusFeeler()
    feeler.feed(ADDRESS, FunctionToolCallEvent(ToolCallPart("read", {}, "tool-1")))
    assert await feeler.next() == "Reading…"
    feeler.feed(ADDRESS, FunctionToolCallEvent(ToolCallPart("search", {}, "tool-2")))
    assert await feeler.next() == "Juggling 2 tools…"
    feeler.feed(
        ADDRESS, FunctionToolResultEvent(ToolReturnPart("read", "done", "tool-1"))
    )
    assert await feeler.next() == "Search…"
    feeler.feed(
        ADDRESS, FunctionToolResultEvent(ToolReturnPart("search", "done", "tool-2"))
    )
    feeler.feed(
        ADDRESS, SubagentSettledEvent(invocation_id="child", status="completed")
    )
    await feeler.close()
    assert await feeler.next() == ""
    assert feeler.presented.empty()


def test_a_phrase_is_drawn_when_an_activity_begins_and_held_while_it_lasts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    drawn = count()
    monkeypatch.setattr(status, "choice", lambda pool: pool[next(drawn) % len(pool)])
    narrator = StatusNarrator()
    thinking = PartDeltaEvent(index=0, delta=ThinkingPartDelta(content_delta="private"))
    first = narrator.narrate(thinking)
    assert narrator.narrate(thinking) == first
    narrator.narrate(PartStartEvent(index=1, part=TextPart(content="answer")))
    again = narrator.narrate(thinking)
    assert again in status.PHRASES["thinking"]
    assert again != first


def test_the_opening_beat_does_not_talk_over_who_picked_the_run_up() -> None:
    narrator = StatusNarrator()
    started = RunStartedEvent(address=ADDRESS, agent_tentacle_id="claude")
    working = RunActivityChangedEvent(activity="working")
    assert narrator.narrate(started) == "Claude is on it"
    assert narrator.narrate(working) == "Claude is on it"
    narrator.narrate(RunActivityChangedEvent(activity="thinking"))
    assert narrator.narrate(working) == "Working on it"


@pytest.mark.parametrize(
    ("spell", "expected"),
    [
        ("summon", "Summoning"),
        ("teleport", "Teleporting"),
        ("scheme", "Scheming"),
        (None, "Moving"),
    ],
)
def test_a_move_is_named_by_its_spell(
    spell: GatewayAction | None, expected: str
) -> None:
    moving = ReflexPhaseChangedEvent(phase="moving", spell=spell)
    assert StatusNarrator().narrate(moving) == expected


def test_a_wait_names_what_it_waits_for() -> None:
    question = DeferredQuestion(
        tool_name="ask_questions",
        tool_call_id="question-call",
        args={"question": "Which option?"},
    )
    narrator = StatusNarrator()
    asked = ActionBatchEvent(batch_id="batch", blocks_run=True, questions=[question])
    assert narrator.narrate(asked) == "Waiting for your answer"
    asked.blocks_run = False
    assert narrator.narrate(asked) == "Working · Answer requested"


@pytest.mark.parametrize("fallback_label", [None, "\x1b", "   "])
async def test_harness_label_is_bounded_and_missing_text_restores_standard_wording(
    fallback_label: str | None,
) -> None:
    feeler = RecordingStatusFeeler()
    feeler.feed(
        ADDRESS,
        RunActivityChangedEvent(
            activity="thinking", display_text="  Pondering\n" + "x" * 200
        ),
    )
    text = await feeler.next()
    assert text.startswith("Pondering ")
    assert text.endswith("x…")
    assert len(text) == 120
    assert "\n" not in text
    feeler.feed(
        ADDRESS,
        RunActivityChangedEvent(activity="thinking", display_text=fallback_label),
    )
    assert await feeler.next() == "Thinking…"
    await feeler.close()
    assert await feeler.next() == ""


async def test_missing_end_expires_and_same_activity_refreshes_deadline(
    advance_time: Callable[[float], None],
) -> None:
    feeler = RecordingStatusFeeler()
    feeler.feed(ADDRESS, RunActivityChangedEvent(activity="thinking"))
    assert await feeler.next() == "Thinking…"
    advance_time(119)
    feeler.feed(
        ADDRESS,
        PartDeltaEvent(
            index=0, delta=ThinkingPartDelta(content_delta="still thinking")
        ),
    )
    assert await feeler.next() == "Thinking…"
    advance_time(2)
    await asyncio.sleep(0)
    assert feeler.presented.empty()
    advance_time(119)
    assert await feeler.next() == ""
    assert not feeler.queues
    feeler.feed(ADDRESS, RunActivityChangedEvent(activity="writing"))
    assert await feeler.next() == "Writing…"
    await feeler.close()
    assert await feeler.next() == ""


@pytest.mark.parametrize(
    "terminal",
    [
        RunEndedEvent(),
        ReflexAsleepEvent(outcome="cancelled"),
        RunErrorEvent(message="failed", trace_id="trace"),
    ],
)
async def test_terminal_events_clear_without_waiting_for_expiry(
    terminal: WireEvent,
) -> None:
    feeler = RecordingStatusFeeler()
    feeler.feed(ADDRESS, RunStartedEvent(address=ADDRESS))
    assert await feeler.next() == "Starting…"
    feeler.feed(ADDRESS, terminal)
    assert await feeler.next() == ""
    await feeler.close()
    assert feeler.presented.empty()


async def test_a_stale_deadline_cannot_clear_a_new_run(
    advance_time: Callable[[float], None],
) -> None:
    feeler = RecordingStatusFeeler()
    feeler.feed(ADDRESS, RunStartedEvent(address=ADDRESS))
    assert await feeler.next() == "Starting…"
    advance_time(100)
    feeler.feed(ADDRESS, RunEndedEvent())
    assert await feeler.next() == ""
    feeler.feed(ADDRESS, RunActivityChangedEvent(activity="thinking"))
    assert await feeler.next() == "Thinking…"
    advance_time(21)
    await asyncio.sleep(0)
    assert feeler.presented.empty()
    await feeler.close()
    assert await feeler.next() == ""


async def test_pending_hints_coalesce_and_share_one_writer_per_thread() -> None:
    started, release = asyncio.Event(), asyncio.Event()

    class HeldStatusFeeler(RecordingStatusFeeler):
        async def present(self, address: ChannelAddress, text: str) -> None:
            if text == "Thinking…":
                started.set()
                await release.wait()
            await super().present(address, text)

    feeler = HeldStatusFeeler()
    feeler.feed(ADDRESS, RunActivityChangedEvent(activity="thinking"))
    await asyncio.wait_for(started.wait(), 1)
    other_user = replace(ADDRESS, user_id="U2")
    feeler.feed(other_user, RunActivityChangedEvent(activity="writing"))
    feeler.feed(ADDRESS, RunEndedEvent())
    assert len(feeler.tasks) == 1
    release.set()
    await feeler.close()
    assert await feeler.next() == "Thinking…"
    assert await feeler.next() == ""
    assert feeler.presented.empty()


@pytest.mark.parametrize("stream", [False, True])
async def test_siphon_delivers_status_outside_runs_and_with_streaming_disabled(
    stream: bool,
) -> None:
    channel, _ = a_channel(stream=stream)
    feeler = RecordingStatusFeeler()
    channel.feelers.status = feeler
    address = replace(ADDRESS, channel_tentacle_id=channel.id)
    async with drawing(channel):
        channel.octomate.nerve.deliver(
            ChannelTopic(channel.id), Impulse(ReflexAwakeEvent(), address)
        )
        assert await feeler.next() == "Waking…"
        start, *rest = a_run(address)
        channel.octomate.nerve.deliver(ChannelTopic(channel.id), start)
        assert await feeler.next() == "Starting…"
        for impulse in rest:
            channel.octomate.nerve.deliver(ChannelTopic(channel.id), impulse)
        assert await feeler.next() == ""
    assert texts(channel) == ["done"]


async def test_failed_status_presentation_does_not_break_message_delivery(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    channel, _ = a_channel(stream=False)
    feeler = RecordingStatusFeeler()
    channel.feelers.status = feeler
    present = AsyncMock(side_effect=RuntimeError("status unavailable"))
    monkeypatch.setattr(feeler, "present", present)
    async with drawing(channel):
        for impulse in a_run(ADDRESS):
            channel.octomate.nerve.deliver(ChannelTopic(channel.id), impulse)
    assert present.await_count > 0
    assert texts(channel) == ["done"]
    assert not feeler.queues


async def test_terminal_clear_bypasses_pending_label_throttle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(RecordingStatusFeeler, "update_interval", 10.0)
    monkeypatch.setattr(RecordingStatusFeeler, "refresh_interval", float("inf"))
    feeler = RecordingStatusFeeler()
    feeler.feed(ADDRESS, RunActivityChangedEvent(activity="thinking"))
    assert await feeler.next() == "Thinking…"
    feeler.feed(ADDRESS, RunActivityChangedEvent(activity="writing"))
    feeler.feed(ADDRESS, RunEndedEvent())
    assert await feeler.next() == ""
    await feeler.close()
    assert feeler.presented.empty()
