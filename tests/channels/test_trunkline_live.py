"""A console watches a thread over the live socket, and what the nerve publishes
to the thread reaches it."""

from __future__ import annotations

import asyncio

from pydantic_ai.messages import PartDeltaEvent, TextPartDelta
from starlette.types import Message
from starlette.websockets import WebSocket

from octomate import Octomate
from octomate.capabilities.harness.events import wire_event_adapter
from octomate.config.channels import TrunklineChannelConfig
from octomate.schemas.thread import ThreadKey
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
        self.inbound.put_nowait({"type": "websocket.connect"})
        self.websocket = WebSocket(
            {"type": "websocket", "headers": []}, self.inbound.get, self.send
        )

    async def send(self, message: Message) -> None:
        if self.gone:
            raise OSError("connection reset")
        self.sent.append(message)

    def leave(self) -> None:
        self.inbound.put_nowait({"type": "websocket.disconnect", "code": 1000})

    @property
    def events(self) -> list[object]:
        return [
            wire_event_adapter.validate_json(one["text"])
            for one in self.sent
            if one["type"] == "websocket.send"
        ]


async def streaming(socket: ConsoleSocket) -> tuple[Octomate, asyncio.Task[None]]:
    """A console watching `KEY`'s thread through `socket`, subscribed and held."""
    octomate = Octomate()
    channel = TrunklineTentacle(
        "web", octomate, config=TrunklineChannelConfig(agents=["first"])
    )
    task = asyncio.create_task(channel.stream_thread(socket.websocket, KEY))
    # The fake socket never suspends, so one turn of the loop subscribes it.
    await asyncio.sleep(0)
    assert octomate.nerve.subscribers[KEY]
    return octomate, task


async def test_a_threads_events_reach_its_socket() -> None:
    socket = ConsoleSocket()
    octomate, task = await streaming(socket)

    octomate.nerve.publish(KEY, EVENT)
    octomate.nerve.publish(OTHER, EVENT)
    # One turn of the loop pumps it down the socket.
    await asyncio.sleep(0)
    socket.leave()
    await task

    assert socket.events == [EVENT]
    assert octomate.nerve.subscribers == {}


async def test_a_console_that_is_gone_ends_quietly() -> None:
    socket = ConsoleSocket()
    octomate, task = await streaming(socket)
    socket.gone = True

    octomate.nerve.publish(KEY, EVENT)
    await asyncio.sleep(0)
    socket.leave()
    await task

    assert socket.events == []
    assert octomate.nerve.subscribers == {}
