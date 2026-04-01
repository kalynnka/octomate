from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Generic, Literal, TypeVar

from anyio import create_memory_object_stream
from anyio.abc import ObjectReceiveStream, ObjectSendStream

from octomate.schemas.events import MessageEvent
from octomate.schemas.segments import AgentSegment
from octomate.schemas.session import SessionKey

T = TypeVar("T")
R = TypeVar("R")


@dataclass
class ChannelSignal:
    key: SessionKey


@dataclass
class MessageBatch(ChannelSignal):
    events: list[MessageEvent]


@dataclass
class AgentSignal:
    key: SessionKey


@dataclass
class SummonAgent(AgentSignal):
    agent_tag: str
    contents: list[MessageEvent]
    summary: str


@dataclass
class StreamFrame(AgentSignal):
    frame_type: Literal["status", "append", "thinking", "flush"]
    content: str


@dataclass
class SendSegments(AgentSignal):
    target: Any
    segments: list[AgentSegment]


@dataclass
class AskUser(AgentSignal):
    request_id: str
    question: str
    options: list[str] = field(default_factory=list)


@dataclass
class UserAnswer:
    request_id: str
    answer: str


@dataclass
class ConfirmTool(AgentSignal):
    request_id: str
    tool_name: str
    args: dict[str, Any]
    title: str
    description: str
    skill: str
    approvers: list[str] = field(default_factory=list)


@dataclass
class ConfirmResult:
    request_id: str
    approved: bool


@dataclass
class TodoUpdate(AgentSignal):
    items: list[Any]
    existing_ts: str | None = None


@dataclass
class TodoResult(AgentSignal):
    ts: str


@dataclass
class DismissPending(AgentSignal):
    pass


class PendingRequests(Generic[R]):
    futures: dict[str, asyncio.Future[R]]

    def __init__(self) -> None:
        self.futures = {}

    def create(self, request_id: str) -> asyncio.Future[R]:
        future: asyncio.Future[R] = asyncio.get_running_loop().create_future()
        self.futures[request_id] = future
        return future

    def resolve(self, request_id: str, value: R) -> None:
        future = self.futures.pop(request_id, None)
        if future and not future.done():
            future.set_result(value)

    def cancel(self, request_id: str) -> None:
        future = self.futures.pop(request_id, None)
        if future and not future.done():
            future.cancel()


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


class NerveDispatcher(Generic[T]):
    nerve: Nerve[T]
    handlers: dict[type, list[Callable]]

    def __init__(self, nerve: Nerve[T]) -> None:
        self.nerve = nerve
        self.handlers = {}

    def register(self, signal_type: type, handler: Callable) -> None:
        self.handlers.setdefault(signal_type, []).append(handler)

    def on(self, signal_type: type) -> Callable:
        def decorator(handler: Callable) -> Callable:
            self.register(signal_type, handler)
            return handler

        return decorator

    async def run(self) -> None:
        async with asyncio.TaskGroup() as tg:
            async for signal in self.nerve:
                for handler in self.handlers.get(type(signal), []):
                    tg.create_task(handler(signal))
