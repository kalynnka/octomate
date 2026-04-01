from __future__ import annotations

from abc import ABC, abstractmethod
from typing import AsyncIterator, Generic, TypeVar

from anyio import create_memory_object_stream
from anyio.abc import ObjectReceiveStream, ObjectSendStream

T = TypeVar("T")


class Nerve(ABC, Generic[T]):
    @abstractmethod
    async def send(self, item: T) -> None: ...

    @abstractmethod
    async def receive(self) -> T: ...

    @abstractmethod
    def __aiter__(self) -> AsyncIterator[T]: ...

    @abstractmethod
    async def open(self) -> None: ...

    @abstractmethod
    async def close(self) -> None: ...

    @abstractmethod
    def clone(self) -> Nerve[T]: ...


class AnyioNerve(Nerve[T]):
    send_stream: ObjectSendStream[T]
    receive_stream: ObjectReceiveStream[T]

    def __init__(self, buffer_size: int = 64) -> None:
        self.send_stream, self.receive_stream = create_memory_object_stream(buffer_size)

    async def send(self, item: T) -> None:
        await self.send_stream.send(item)

    async def receive(self) -> T:
        return await self.receive_stream.receive()

    def __aiter__(self) -> AsyncIterator[T]:
        return self.receive_stream.__aiter__()

    async def open(self) -> None:
        pass

    async def close(self) -> None:
        await self.send_stream.aclose()
        await self.receive_stream.aclose()

    def clone(self) -> AnyioNerve[T]:
        cloned: AnyioNerve[T] = object.__new__(AnyioNerve)
        cloned.send_stream = self.send_stream.clone()
        cloned.receive_stream = self.receive_stream.clone()
        return cloned
