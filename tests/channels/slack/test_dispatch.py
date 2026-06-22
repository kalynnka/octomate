"""Slack inbound dispatch: `on_message` must run the turn off the socket
listener. bolt acks the socket envelope only after the listener returns, so
awaiting the full run (which can park on an approval) would blow Slack's ~3s ack
window and trigger event re-delivery (duplicate runs). The listener instead
schedules `ingest` as a tracked task and returns immediately.
"""

from __future__ import annotations

import asyncio
from typing import cast

from slack_bolt.async_app import AsyncSay

from octomate.schemas.conversation import UserProfile
from octomate.tentacles.channel.slack.schema import SlackMessageEvent
from tests.channels.slack.fakes import FakeSlackInk, slack_channel


async def test_on_message_does_not_block_on_the_run() -> None:
    channel = slack_channel(FakeSlackInk())
    channel.profile = UserProfile(user_id="bot")
    channel.ingest_tasks = set()

    started = asyncio.Event()
    release = asyncio.Event()

    async def slow_ingest(event: object) -> None:
        started.set()
        await release.wait()

    channel.ingest = slow_ingest  # type: ignore[method-assign]
    event = cast(SlackMessageEvent, {"user": "U1", "text": "hi"})

    # If on_message awaited ingest, this would hang (release is never set yet).
    await channel.on_message(event, cast(AsyncSay, None))
    assert len(channel.ingest_tasks) == 1

    await asyncio.sleep(0)
    assert started.is_set()  # the run proceeds in its own task

    task = next(iter(channel.ingest_tasks))
    release.set()
    await task
    await asyncio.sleep(0)  # let the done-callback run
    assert channel.ingest_tasks == set()  # done-callback cleaned up the reference


async def test_on_message_ignores_bot_and_subtype_events() -> None:
    channel = slack_channel(FakeSlackInk())
    channel.profile = UserProfile(user_id="bot")
    channel.ingest_tasks = set()

    calls: list[object] = []

    async def record_ingest(event: object) -> None:
        calls.append(event)

    channel.ingest = record_ingest  # type: ignore[method-assign]

    await channel.on_message(
        cast(SlackMessageEvent, {"subtype": "bot_message"}), cast(AsyncSay, None)
    )
    await channel.on_message(
        cast(SlackMessageEvent, {"user": "bot"}), cast(AsyncSay, None)
    )
    await asyncio.sleep(0)

    assert calls == []
    assert channel.ingest_tasks == set()
