"""Buffered stream ends backed by memory or by a duplex stdio connection."""

from __future__ import annotations

import asyncio
from contextlib import suppress
from dataclasses import dataclass, field

import anyio
from anyio.abc import AnyByteReceiveStream, AnyByteSendStream
from anyio.streams.buffered import BufferedByteReceiveStream
from anyio.streams.memory import MemoryObjectReceiveStream, MemoryObjectSendStream
from pydantic import TypeAdapter, ValidationError

FIBER_BUFFER = 1024
MAX_FRAME_BYTES = 1024 * 1024


class FiberError(RuntimeError):
    """A fiber failure safe to report without exposing a peer's payload."""


@dataclass(eq=False)
class Fiber[ItemT]:
    """Buffered send and receive ends; each implementation owns their connection."""

    send: MemoryObjectSendStream[ItemT]
    receive: MemoryObjectReceiveStream[ItemT]
    failure: FiberError | None = field(default=None, init=False)
    closed: bool = field(default=False, init=False)

    def close(self) -> None:
        self.closed = True
        self.send.close()
        self.receive.close()

    def fail(self, error: FiberError) -> None:
        self.failure = error
        self.close()


class MemoryFiber[ItemT](Fiber[ItemT]):
    """An in-process publisher or subscription, with bounded optional buffering."""

    def __init__(self, buffer: float = FIBER_BUFFER) -> None:
        send, receive = anyio.create_memory_object_stream[ItemT](buffer)
        super().__init__(send, receive)

    def fail(self, error: FiberError) -> None:
        """End delivery while allowing the subscriber to drain its backlog."""
        self.failure = error
        self.closed = True
        self.send.close()


class StdioFiber[ItemT](Fiber[ItemT]):
    """Encode outgoing items and decode incoming items on independent stdio pipes.

    Framing and I/O belong here. The fiber has no knowledge of nerve topics,
    subscribers or routing, and never loops its own outgoing items back locally.
    """

    input: BufferedByteReceiveStream
    output: AnyByteSendStream
    adapter: TypeAdapter[ItemT]
    incoming: MemoryObjectSendStream[ItemT]
    outgoing: MemoryObjectReceiveStream[ItemT]
    task: asyncio.Task[None] | None

    def __init__(
        self,
        receive: AnyByteReceiveStream,
        send: AnyByteSendStream,
        adapter: TypeAdapter[ItemT],
    ) -> None:
        outgoing_send, self.outgoing = anyio.create_memory_object_stream[ItemT](
            FIBER_BUFFER
        )
        self.incoming, incoming_receive = anyio.create_memory_object_stream[ItemT](
            FIBER_BUFFER
        )
        super().__init__(outgoing_send, incoming_receive)
        self.input = BufferedByteReceiveStream(receive)
        self.output = send
        self.adapter = adapter
        self.task = None

    def start(self) -> asyncio.Task[None]:
        if self.task is not None or self.closed:
            raise FiberError("Stdio fiber cannot be started twice")
        self.task = asyncio.create_task(self.run())
        return self.task

    async def read(self) -> None:
        while True:
            try:
                line = await self.input.receive_until(b"\n", MAX_FRAME_BYTES + 1)
            except anyio.IncompleteRead:
                if self.input.buffer:
                    raise FiberError("Incomplete stdio frame") from None
                return
            except anyio.DelimiterNotFound:
                raise FiberError("Stdio frame exceeds the size limit") from None
            if len(line) > MAX_FRAME_BYTES:
                raise FiberError("Stdio frame exceeds the size limit")
            try:
                item = self.adapter.validate_json(line)
            except ValidationError:
                raise FiberError("Invalid stdio frame") from None
            await self.incoming.send(item)

    async def write(self) -> None:
        async for item in self.outgoing:
            frame = self.adapter.dump_json(item, warnings=False)
            if len(frame) > MAX_FRAME_BYTES:
                raise FiberError("Stdio frame exceeds the size limit")
            await self.output.send(frame + b"\n")

    async def run(self) -> None:
        try:
            async with asyncio.TaskGroup() as tasks:
                tasks.create_task(self.write())
                try:
                    await self.read()
                finally:
                    self.send.close()
        except ExceptionGroup:
            self.failure = self.failure or FiberError("Stdio fiber connection failed")
        finally:
            await self.close_streams()

    def disconnect(self) -> None:
        self.closed = True
        self.send.close()
        self.outgoing.close()
        self.incoming.close()

    def close(self) -> None:
        self.disconnect()
        if self.task is not None and not self.task.done():
            self.task.cancel()

    async def close_streams(self) -> None:
        self.disconnect()
        await self.output.aclose()
        await self.input.aclose()

    async def wait_closed(self) -> None:
        if self.task is None:
            raise FiberError("Stdio fiber has not been started")
        try:
            await asyncio.shield(self.task)
        except asyncio.CancelledError:
            if not self.task.cancelled() or self.failure is None:
                raise
        if self.failure is not None:
            raise self.failure

    async def aclose(self) -> None:
        self.close()
        if self.task is not None:
            with suppress(asyncio.CancelledError):
                await self.task
        await self.close_streams()
        self.receive.close()
