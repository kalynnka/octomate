"""A console watches a thread over the live socket, and what the nerve publishes
to the thread reaches it."""

from __future__ import annotations

import asyncio

import pytest
from pydantic_ai.messages import PartDeltaEvent, TextPartDelta
from starlette.types import Message
from starlette.websockets import WebSocket
from uuid_utils.compat import uuid7

from octomate import Octomate
from octomate.capabilities.harness.events import wire_event_adapter
from octomate.config.channels import TrunklineChannelConfig
from octomate.nerve import Impulse, ThreadTopic, Topic, UserTopic
from octomate.schemas.thread import ThreadKey
from octomate.tentacles.trunkline import base as trunkline_base
from octomate.tentacles.trunkline.base import TrunklineTentacle

KEY = ThreadKey("web", "thread", "lu", "t1")
OTHER = ThreadKey("web", "thread", "lu", "t2")
EVENT = PartDeltaEvent(index=0, delta=TextPartDelta(content_delta="hi"))


class ConsoleSocket:
    """A console's websocket as the server sees it: what the console sends is
    queued in, and what the server sends is kept."""

    def __init__(self) -> None:
        self.inbound: asyncio.Queue[Message] = asyncio.Queue()
        self.sent: list[Message] = []
        self.gone = False  # whether the peer is gone, so a send fails
        self.closed = asyncio.Event()  # set once the server closes the socket
        self.inbound.put_nowait({"type": "websocket.connect"})
        self.websocket = WebSocket(
            {"type": "websocket", "headers": []}, self.inbound.get, self.send
        )

    async def send(self, message: Message) -> None:
        if self.gone:
            raise OSError("connection reset")
        self.sent.append(message)
        if message["type"] == "websocket.close":
            self.closed.set()

    def leave(self) -> None:
        self.inbound.put_nowait({"type": "websocket.disconnect", "code": 1000})

    @property
    def events(self) -> list[object]:
        return [
            wire_event_adapter.validate_json(one["text"])
            for one in self.sent
            if one["type"] == "websocket.send"
        ]


async def streaming(
    socket: ConsoleSocket, topic: Topic
) -> tuple[Octomate, asyncio.Task[None]]:
    """A console watching `topic` through `socket`, subscribed and held."""
    octomate = Octomate()
    channel = TrunklineTentacle(
        "web", octomate, config=TrunklineChannelConfig(agents=["first"])
    )
    task = asyncio.create_task(channel.siphon.watch(socket.websocket, topic))
    # The fake socket never suspends, so one turn of the loop subscribes it.
    await asyncio.sleep(0)
    assert octomate.nerve.subscribers[topic]
    return octomate, task


async def test_a_threads_events_reach_its_socket() -> None:
    socket = ConsoleSocket()
    octomate, task = await streaming(socket, ThreadTopic(KEY))

    octomate.nerve.deliver(ThreadTopic(KEY), Impulse(EVENT, KEY.address("lu")))
    octomate.nerve.deliver(ThreadTopic(OTHER), Impulse(EVENT, OTHER.address("lu")))
    # One turn of the loop pumps it down the socket.
    await asyncio.sleep(0)
    socket.leave()
    await task

    assert socket.events == [EVENT]
    assert octomate.nerve.subscribers == {}


async def test_a_console_that_is_gone_ends_quietly() -> None:
    socket = ConsoleSocket()
    octomate, task = await streaming(socket, ThreadTopic(KEY))
    socket.gone = True

    octomate.nerve.deliver(ThreadTopic(KEY), Impulse(EVENT, KEY.address("lu")))
    await asyncio.sleep(0)
    socket.leave()
    await task

    assert socket.events == []
    assert octomate.nerve.subscribers == {}


@pytest.mark.parametrize("topic", [ThreadTopic(KEY), UserTopic(uuid7())])
async def test_a_console_that_fell_behind_is_closed_to_read_the_thread_again(
    monkeypatch: pytest.MonkeyPatch, topic: Topic
) -> None:
    monkeypatch.setattr(trunkline_base, "CONSOLE_BACKLOG", 1)
    socket = ConsoleSocket()
    octomate, task = await streaming(socket, topic)

    # Nothing is read in between, so the second finds the backlog full.
    for _ in range(2):
        octomate.nerve.deliver(topic, Impulse(EVENT, KEY.address("lu")))
    await asyncio.wait_for(socket.closed.wait(), 1)
    socket.leave()
    await task

    assert socket.events == [EVENT]
    assert socket.sent[-1] == {
        "type": "websocket.close",
        "code": 1013,
        "reason": "fell behind",
    }
