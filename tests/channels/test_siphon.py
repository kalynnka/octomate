"""A channel's siphon draws what the nerve carries to it: each thread in order in a
lane of its own, a run as a timeline or quietly, and every recorded row marked
with the platform id it was rendered as."""

from __future__ import annotations

import asyncio
from contextlib import nullcontext

import pytest
from anyio.abc import ObjectReceiveStream
from pydantic_ai.usage import RunUsage

from octomate.capabilities.harness.events import (
    MessageSentEvent,
    RunEndedEvent,
    RunResultEvent,
    RunStartedEvent,
)
from octomate.config import ChannelConfig, ChannelStreamConfig
from octomate.nerve import ChannelTopic, Impulse
from octomate.schemas.conversation import ChannelAddress
from octomate.schemas.segments import MarkdownSegment, MessageSegment
from octomate.schemas.thread import ThreadMessage
from octomate.tentacles.feelers.output import IMMessageID, MarkdownFeeler
from tests.support.channels import FakeChannelTentacle
from tests.support.managers import FakeThreadManager
from tests.support.nerve import drawing

ALICE = ChannelAddress("im", "dm", "alice", "alice")
BOB = ChannelAddress("im", "dm", "bob", "bob")


def a_channel(*, stream: bool) -> tuple[FakeChannelTentacle, FakeThreadManager]:
    """A channel whose host keeps its ledger in memory, for the siphon to mark."""
    channel = FakeChannelTentacle(
        config=ChannelConfig(
            type="fake", stream=ChannelStreamConfig(enabled=stream), agents=["inkling"]
        )
    )
    threads = FakeThreadManager()
    channel.octomate.threads = threads
    return channel, threads


async def a_reply(threads: FakeThreadManager, address: ChannelAddress) -> ThreadMessage:
    """The row the graph records for a run's reply before the channel renders it."""
    return await threads.record_outbound(
        address,
        agent_tentacle_id="inkling",
        segments=[MarkdownSegment(data={"text": "done"})],
        sender=FakeChannelTentacle().self_profile,
    )


def a_run(
    address: ChannelAddress,
    *between: MessageSentEvent,
    answer: str = "done",
    reply: ThreadMessage | None = None,
) -> list[Impulse]:
    events = [
        RunStartedEvent(address=address),
        *between,
        RunResultEvent(output=answer, usage=RunUsage()),
        RunEndedEvent(thread_message_id=reply.id if reply is not None else None),
    ]
    return [Impulse(event, address) for event in events]


def sent(text: str) -> MessageSentEvent:
    return MessageSentEvent(segments=[MarkdownSegment(data={"text": text})])


def texts(channel: FakeChannelTentacle) -> list[str]:
    return [
        message["text"] for _, _, messages, *_ in channel.sent for message in messages
    ]


@pytest.mark.parametrize("failed", [False, True])
async def test_context_owns_subscription_and_reader(failed: bool) -> None:
    channel, _ = a_channel(stream=False)
    topic = ChannelTopic(channel.id)
    with pytest.raises(RuntimeError, match="body failed") if failed else nullcontext():
        async with channel.siphon as siphon:
            assert siphon is channel.siphon
            reading = siphon.reading
            assert reading is not None
            assert topic in channel.octomate.nerve.subscribers
            if failed:
                raise RuntimeError("body failed")
    assert reading.done()
    assert topic not in channel.octomate.nerve.subscribers


async def test_reader_failure_still_closes_the_subscription(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    channel, _ = a_channel(stream=False)
    started = asyncio.Event()

    async def read(impulses: ObjectReceiveStream[Impulse]) -> None:
        started.set()
        raise RuntimeError("reader failed")

    monkeypatch.setattr(channel.siphon, "read", read)
    with pytest.raises(RuntimeError, match="reader failed"):
        async with channel.siphon:
            await started.wait()
    assert not channel.octomate.nerve.subscribers


async def test_cancelled_context_closes_subscription_and_reader() -> None:
    channel, _ = a_channel(stream=False)
    entered = asyncio.Event()

    async def run() -> None:
        async with channel.siphon:
            entered.set()
            await asyncio.Future[None]()

    task = asyncio.create_task(run())
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert channel.siphon.reading is not None
    assert channel.siphon.reading.done()
    assert not channel.octomate.nerve.subscribers


async def test_a_streaming_channel_draws_a_run_on_its_timeline_and_marks_it() -> None:
    channel, threads = a_channel(stream=True)
    reply = await a_reply(threads, ALICE)

    async with drawing(channel):
        for impulse in a_run(ALICE, reply=reply):
            channel.octomate.nerve.deliver(ChannelTopic(channel.id), impulse)

    [(where, rendered)] = channel.consumed
    assert where == ALICE
    assert texts(channel) == ["done"]
    assert rendered is not None
    assert reply.platform_message_id == rendered


async def test_a_quiet_channel_posts_its_sends_then_the_reply_once() -> None:
    channel, threads = a_channel(stream=False)
    reply = await a_reply(threads, ALICE)

    async with drawing(channel):
        for impulse in a_run(ALICE, sent("working on it"), reply=reply):
            channel.octomate.nerve.deliver(ChannelTopic(channel.id), impulse)

    assert channel.consumed == []
    assert texts(channel) == ["working on it", "done"]
    assert reply.platform_message_id == "sent-2"


class HeldMarkdownFeeler:
    """The channel's own markdown, held for one chat until released."""

    def __init__(self, posting: MarkdownFeeler, held: ChannelAddress) -> None:
        self.posting = posting
        self.held = held
        self.release = asyncio.Event()
        self.posted_elsewhere = asyncio.Event()

    async def present(
        self, address: ChannelAddress, markdown: str
    ) -> IMMessageID | None:
        if address == self.held:
            await self.release.wait()
            return await self.posting.present(address, markdown)
        posted = await self.posting.present(address, markdown)
        self.posted_elsewhere.set()
        return posted


async def test_a_slow_thread_holds_up_no_other() -> None:
    channel, _ = a_channel(stream=False)
    held = HeldMarkdownFeeler(channel.feelers.markdown, ALICE)
    channel.feelers.markdown = held

    async with drawing(channel):
        for impulse in [*a_run(ALICE, answer="alice's"), *a_run(BOB, answer="bob's")]:
            channel.octomate.nerve.deliver(ChannelTopic(channel.id), impulse)
        await asyncio.wait_for(held.posted_elsewhere.wait(), 1)
        assert texts(channel) == ["bob's"]
        held.release.set()

    assert texts(channel) == ["bob's", "alice's"]


class FlakySegmentsFeeler:
    """Fails the first message it is handed, and posts the rest as the channel's
    own would."""

    def __init__(self, channel: FakeChannelTentacle) -> None:
        self.posting = channel.feelers.segments
        self.failed = False

    async def present(
        self, address: ChannelAddress, segments: list[MessageSegment]
    ) -> IMMessageID | None:
        if not self.failed:
            self.failed = True
            raise RuntimeError("the platform said no")
        return await self.posting.present(address, segments)


async def test_a_rendering_that_fails_leaves_its_thread_drawing() -> None:
    channel, _ = a_channel(stream=False)
    channel.feelers.segments = FlakySegmentsFeeler(channel)

    async with drawing(channel):
        for impulse in [Impulse(sent("lost"), ALICE), Impulse(sent("kept"), ALICE)]:
            channel.octomate.nerve.deliver(ChannelTopic(channel.id), impulse)

    assert texts(channel) == ["kept"]
