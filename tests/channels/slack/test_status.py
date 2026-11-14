"""Slack status consumes the event stream separately from timeline rendering."""

from __future__ import annotations

import asyncio

import pytest
from pydantic_ai.messages import (
    FunctionToolCallEvent,
    PartStartEvent,
    TextPart,
    ThinkingPart,
    ToolCallPart,
)

from octomate.events import (
    ReflexAwakeEvent,
    RunActivityChangedEvent,
    RunEndedEvent,
    RunStartedEvent,
)
from octomate.nerve import ChannelTopic, Impulse
from tests.channels.slack.fakes import (
    FakeSlackInk,
    FakeSlackStream,
    slack_channel,
    slack_key,
)
from tests.support.nerve import drawing


class ObservedSlackInk(FakeSlackInk):
    def __init__(self) -> None:
        super().__init__()
        self.status_updates: asyncio.Queue[str] = asyncio.Queue()

    async def set_assistant_status(
        self, channel: str, thread_ts: str, status: str
    ) -> None:
        assert channel == "C1"
        assert thread_ts == "1710000000.000100"
        await super().set_assistant_status(channel, thread_ts, status)
        self.status_updates.put_nowait(status)

    async def next_status(self) -> str:
        return await asyncio.wait_for(self.status_updates.get(), 1)


@pytest.mark.parametrize("stream", [False, True])
async def test_slack_status_follows_events_before_and_during_a_run(
    stream: bool,
) -> None:
    ink = ObservedSlackInk()
    channel = slack_channel(ink)
    channel.config.stream.enabled = stream
    address = slack_key()
    async with drawing(channel):
        topic = ChannelTopic(channel.id)
        channel.octomate.nerve.deliver(topic, Impulse(ReflexAwakeEvent(), address))
        assert await ink.next_status() == "Preparing…"
        channel.octomate.nerve.deliver(
            topic, Impulse(RunStartedEvent(address=address), address)
        )
        assert await ink.next_status() == "Working…"
        channel.octomate.nerve.deliver(
            topic,
            Impulse(
                PartStartEvent(index=0, part=ThinkingPart(content="checking")), address
            ),
        )
        assert await ink.next_status() == "Thinking…"
        channel.octomate.nerve.deliver(
            topic,
            Impulse(
                FunctionToolCallEvent(ToolCallPart("lookup", {}, "call-1")), address
            ),
        )
        assert await ink.next_status() == "Lookup…"
        channel.octomate.nerve.deliver(
            topic,
            Impulse(PartStartEvent(index=1, part=TextPart(content="answer")), address),
        )
        assert await ink.next_status() == "Writing the response…"
        channel.octomate.nerve.deliver(topic, Impulse(RunEndedEvent(), address))
        assert await ink.next_status() == ""
    assert ink.statuses[-1] == ""


async def test_harness_status_escapes_slack_mentions_and_restores_standard_label() -> (
    None
):
    ink = ObservedSlackInk()
    channel = slack_channel(ink)
    status = channel.feelers.status
    assert status is not None
    try:
        status.feed(
            slack_key(),
            RunActivityChangedEvent(
                activity="thinking",
                display_text="Pondering <@U1> & <https://example.com>",
            ),
        )
        assert (
            await ink.next_status()
            == "Pondering &lt;@U1&gt; &amp; &lt;https://example.com&gt;"
        )
        status.feed(slack_key(), RunActivityChangedEvent(activity="thinking"))
        assert await ink.next_status() == "Thinking…"
    finally:
        await status.close()
    assert await ink.next_status() == ""


async def test_slow_timeline_cannot_delay_terminal_status() -> None:
    started, release = asyncio.Event(), asyncio.Event()

    class HeldTextInk(ObservedSlackInk):
        async def start_stream(
            self,
            channel: str,
            thread_ts: str,
            *,
            recipient_user_id: str | None = None,
            recipient_team_id: str | None = None,
            task_display_mode: str | None = None,
        ) -> FakeSlackStream:
            started.set()
            await release.wait()
            return await super().start_stream(
                channel,
                thread_ts,
                recipient_user_id=recipient_user_id,
                recipient_team_id=recipient_team_id,
                task_display_mode=task_display_mode,
            )

    ink = HeldTextInk()
    channel = slack_channel(ink)
    address = slack_key()
    async with drawing(channel):
        topic = ChannelTopic(channel.id)
        channel.octomate.nerve.deliver(
            topic, Impulse(RunStartedEvent(address=address), address)
        )
        assert await ink.next_status() == "Working…"
        channel.octomate.nerve.deliver(
            topic,
            Impulse(PartStartEvent(index=0, part=TextPart(content="answer")), address),
        )
        await asyncio.wait_for(started.wait(), 1)
        assert await ink.next_status() == "Writing the response…"
        try:
            channel.octomate.nerve.deliver(topic, Impulse(RunEndedEvent(), address))
            assert await ink.next_status() == ""
            assert channel.siphon.lanes
        finally:
            release.set()
    assert ink.statuses[-1] == ""
