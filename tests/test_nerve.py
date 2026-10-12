"""What is published to a thread reaches its subscribers, and waits for none."""

from __future__ import annotations

import pytest
from anyio import WouldBlock
from pydantic_ai.messages import PartDeltaEvent, TextPartDelta

from octomate import Octomate
from octomate.config.channels import TrunklineChannelConfig
from octomate.nerve import SUBSCRIBER_BUFFER, Nerve
from octomate.schemas.conversation import ChannelAddress
from octomate.schemas.thread import ThreadKey
from octomate.tentacles.trunkline.base import TrunklineTentacle

KEY = ThreadKey("web", "thread", "lu", "t1")
OTHER = ThreadKey("web", "thread", "lu", "t2")
EVENT = PartDeltaEvent(index=0, delta=TextPartDelta(content_delta="hi"))


def test_a_threads_events_reach_its_subscriber() -> None:
    nerve = Nerve()
    with nerve.subscribe(KEY) as events:
        nerve.publish(KEY, EVENT)
        nerve.publish(OTHER, EVENT)

        assert events.receive_nowait() == EVENT
        with pytest.raises(WouldBlock):
            events.receive_nowait()
    assert nerve.subscribers == {}


def test_every_subscriber_of_the_thread_gets_its_events() -> None:
    nerve = Nerve()
    with nerve.subscribe(KEY) as first, nerve.subscribe(KEY) as second:
        nerve.publish(KEY, EVENT)

        assert first.receive_nowait() == EVENT
        assert second.receive_nowait() == EVENT


def test_a_subscriber_that_fell_behind_misses_what_did_not_fit() -> None:
    nerve = Nerve()
    with nerve.subscribe(KEY) as events:
        for _ in range(SUBSCRIBER_BUFFER + 1):
            nerve.publish(KEY, EVENT)

        assert events.statistics().current_buffer_used == SUBSCRIBER_BUFFER


def test_publishing_to_a_thread_nobody_subscribes_to_is_nothing() -> None:
    Nerve().publish(KEY, EVENT)


async def test_a_trunkline_thread_publishes_its_run_stream() -> None:
    octomate = Octomate()
    channel = TrunklineTentacle(
        "web", octomate, config=TrunklineChannelConfig(agents=["first"])
    )
    address = ChannelAddress("web", "thread", "lu", "lu", channel_thread_id="t1")
    with octomate.nerve.subscribe(KEY) as events:
        await channel.forward(address, EVENT)

        assert events.receive_nowait() == EVENT
