"""One-way buffered streams backed by memory or one end of a stdio pipe."""

from __future__ import annotations

import asyncio
from contextlib import suppress
from dataclasses import dataclass, field

import anyio
from anyio.abc import (
    AnyByteReceiveStream,
    AnyByteSendStream,
    ObjectReceiveStream,
    ObjectSendStream,
)
from anyio.streams.buffered import BufferedByteReceiveStream
from anyio.streams.memory import MemoryObjectReceiveStream, MemoryObjectSendStream
from pydantic import TypeAdapter, ValidationError

FIBER_BUFFER = 1024
MAX_FRAME_BYTES = 1024 * 1024


class FiberError(RuntimeError):
    """A fiber failure safe to report without exposing a peer's payload."""


@dataclass(eq=False)
class Fiber[ItemT](ObjectSendStream[ItemT], ObjectReceiveStream[ItemT]):
    """One stream from a producer to a consumer, with bounded optional buffering."""

    sender: MemoryObjectSendStream[ItemT]
    receiver: MemoryObjectReceiveStream[ItemT]
    failure: FiberError | None = field(default=None, init=False)
    closed: bool = field(default=False, init=False)

    async def send(self, item: ItemT) -> None:
        await self.sender.send(item)

    def send_nowait(self, item: ItemT) -> None:
        self.sender.send_nowait(item)

    async def receive(self) -> ItemT:
        return await self.receiver.receive()

    def receive_nowait(self) -> ItemT:
        return self.receiver.receive_nowait()

    def close_send(self) -> None:
        """End production while allowing the consumer to drain queued items."""
        self.sender.close()

    def close(self) -> None:
        self.closed = True
        self.sender.close()
        self.receiver.close()

    async def aclose(self) -> None:
        self.close()

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
        self.close_send()


class StdioFiber[ItemT](Fiber[ItemT]):
    """One local endpoint: receive from an input pipe or send to an output pipe."""

    input: BufferedByteReceiveStream | None
    output: AnyByteSendStream | None
    adapter: TypeAdapter[ItemT]
    task: asyncio.Task[None] | None

    def __init__(
        self,
        adapter: TypeAdapter[ItemT],
        *,
        input: AnyByteReceiveStream | None = None,
        output: AnyByteSendStream | None = None,
    ) -> None:
        if (input is None) == (output is None):
            raise ValueError(
                "A stdio fiber requires exactly one input or output stream"
            )
        sender, receiver = anyio.create_memory_object_stream[ItemT](FIBER_BUFFER)
        super().__init__(sender, receiver)
        self.input = BufferedByteReceiveStream(input) if input is not None else None
        self.output = output
        self.adapter = adapter
        self.task = None

    async def send(self, item: ItemT) -> None:
        if self.output is None:
            raise FiberError("This stdio fiber has no local sending endpoint")
        await super().send(item)

    def send_nowait(self, item: ItemT) -> None:
        if self.output is None:
            raise FiberError("This stdio fiber has no local sending endpoint")
        super().send_nowait(item)

    async def receive(self) -> ItemT:
        if self.input is None:
            raise FiberError("This stdio fiber has no local receiving endpoint")
        return await super().receive()

    def receive_nowait(self) -> ItemT:
        if self.input is None:
            raise FiberError("This stdio fiber has no local receiving endpoint")
        return super().receive_nowait()

    def start(self) -> asyncio.Task[None]:
        if self.task is not None or self.closed:
            raise FiberError("Stdio fiber cannot be started twice")
        self.task = asyncio.create_task(self.run())
        return self.task

    async def read(self) -> None:
        if self.input is None:
            raise FiberError("This stdio fiber has no input stream")
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
            await self.sender.send(item)

    async def write(self) -> None:
        if self.output is None:
            raise FiberError("This stdio fiber has no output stream")
        async for item in self.receiver:
            frame = self.adapter.dump_json(item, warnings=False)
            if len(frame) > MAX_FRAME_BYTES:
                raise FiberError("Stdio frame exceeds the size limit")
            await self.output.send(frame + b"\n")

    async def run(self) -> None:
        try:
            if self.input is not None:
                await self.read()
            else:
                await self.write()
        except Exception:
            self.failure = self.failure or FiberError("Stdio fiber connection failed")
        finally:
            await self.close_streams()

    def disconnect(self) -> None:
        self.closed = True
        self.close_send()

    def close(self) -> None:
        self.disconnect()
        if self.task is not None and not self.task.done():
            self.task.cancel()

    async def close_streams(self) -> None:
        self.disconnect()
        if self.output is not None:
            await self.output.aclose()
        if self.input is not None:
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
        self.receiver.close()
