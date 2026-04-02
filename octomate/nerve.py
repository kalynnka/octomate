from __future__ import annotations

import asyncio
import contextlib
import logging
from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Generic, Literal, TypeVar

from anyio import create_memory_object_stream
from anyio.streams.memory import MemoryObjectReceiveStream, MemoryObjectSendStream

from octomate.schemas.events import MessageEvent
from octomate.schemas.segments import AgentSegment
from octomate.schemas.session import SessionKey

logger = logging.getLogger(__name__)

T = TypeVar("T")
R = TypeVar("R")


@dataclass
class ChannelSignal:
    key: SessionKey


@dataclass
class MessageBatch(ChannelSignal):
    events: list[MessageEvent]
    kind: Literal["message_batch"] = field(default="message_batch", init=False)


@dataclass
class AgentSignal:
    key: SessionKey


@dataclass
class SummonAgent(AgentSignal):
    agent_tag: str
    contents: list[MessageEvent]
    summary: str
    kind: Literal["summon_agent"] = field(default="summon_agent", init=False)


@dataclass
class StreamFrame(AgentSignal):
    frame_type: Literal["status", "append", "thinking", "flush", "close"]
    content: str
    kind: Literal["stream_frame"] = field(default="stream_frame", init=False)


@dataclass
class SendSegments(AgentSignal):
    segments: list[AgentSegment]
    kind: Literal["send_segments"] = field(default="send_segments", init=False)


@dataclass
class AskUser(AgentSignal):
    request_id: str
    question: str
    options: list[str] = field(default_factory=list)
    multi_select: bool = False
    kind: Literal["ask_user"] = field(default="ask_user", init=False)


@dataclass
class UserAnswer(AgentSignal):
    request_id: str
    answer: str
    kind: Literal["user_answer"] = field(default="user_answer", init=False)


@dataclass
class ConfirmTool(AgentSignal):
    request_id: str
    tool_name: str
    args: dict[str, Any]
    title: str
    description: str
    skill: str
    approvers: list[str] = field(default_factory=list)
    kind: Literal["confirm_tool"] = field(default="confirm_tool", init=False)


@dataclass
class ConfirmResult(AgentSignal):
    request_id: str
    approved: bool
    kind: Literal["confirm_result"] = field(default="confirm_result", init=False)


@dataclass
class TodoUpdate(AgentSignal):
    items: list[Any]
    # Current todo card reference held by the agent tentacle, if any.
    # Provided so the channel can update an existing card rather than posting a new one.
    existing_card_ref: str | None = None
    kind: Literal["todo_update"] = field(default="todo_update", init=False)


@dataclass
class TodoResult(AgentSignal):
    # Platform-agnostic reference to the posted/updated todo card (e.g. message ID).
    card_ref: str
    kind: Literal["todo_result"] = field(default="todo_result", init=False)


@dataclass
class DismissPending(AgentSignal):
    # Current todo card reference to unpin, if any (provided by the agent tentacle).
    card_ref: str | None = None
    kind: Literal["dismiss_pending"] = field(default="dismiss_pending", init=False)


@dataclass
class ReleaseThread(AgentSignal):
    """Return thread ownership to the channel tentacle."""

    kind: Literal["release_thread"] = field(default="release_thread", init=False)


# Union of every signal type that travels through the agent nerve.
# The `kind` discriminator on each class enables Pydantic-based deserialization
# (e.g. for a future Redis transport):
#   Annotated[AgentSignalType, Field(discriminator="kind")]
AgentSignalType = (
    SummonAgent
    | StreamFrame
    | SendSegments
    | AskUser
    | UserAnswer
    | ConfirmTool
    | ConfirmResult
    | TodoUpdate
    | TodoResult
    | DismissPending
    | ReleaseThread
)


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


class AgentPending:
    """Groups the three request/response PendingRequests used by agent tentacles."""

    answers: PendingRequests[UserAnswer]
    confirmations: PendingRequests[ConfirmResult]
    todos: PendingRequests[TodoResult]

    def __init__(self) -> None:
        self.answers = PendingRequests()
        self.confirmations = PendingRequests()
        self.todos = PendingRequests()


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
    send_stream: MemoryObjectSendStream[T]
    receive_stream: MemoryObjectReceiveStream[T]

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
            async def safe_call(signal: T) -> None:
                try:
                    await handler(signal)
                except Exception:
                    logger.exception(
                        "NerveDispatcher: error in handler for %s",
                        type(signal).__name__,
                    )

            self.register(signal_type, safe_call)
            return handler

        return decorator

    async def run(self) -> None:
        async with asyncio.TaskGroup() as tg:
            async for signal in self.nerve:
                for handler in self.handlers.get(type(signal), []):
                    tg.create_task(handler(signal))


class NerveStream:
    """StreamSink-compatible object that forwards streaming calls as StreamFrame signals.

    Use as an async context manager; sends a ``close`` frame on exit so the
    octopus-side handler can tear down the real StreamSink.
    """

    nerve: Nerve[Any]
    key: SessionKey

    def __init__(self, nerve: Nerve[Any], key: SessionKey) -> None:
        self.nerve = nerve
        self.key = key

    async def set_status(self, status: str) -> None:
        await self.nerve.send(
            StreamFrame(key=self.key, frame_type="status", content=status)
        )

    async def append(self, text: str) -> None:
        await self.nerve.send(
            StreamFrame(key=self.key, frame_type="append", content=text)
        )

    async def post_thinking_block(self, text: str) -> None:
        if text:
            await self.nerve.send(
                StreamFrame(key=self.key, frame_type="thinking", content=text)
            )

    async def flush(self) -> None:
        await self.nerve.send(StreamFrame(key=self.key, frame_type="flush", content=""))

    async def __aenter__(self) -> NerveStream:
        return self

    async def __aexit__(self, *_: Any) -> None:
        await self.nerve.send(StreamFrame(key=self.key, frame_type="close", content=""))


class SinkRegistry:
    """Manages open StreamSink contexts keyed by SessionKey.

    Acts as a lazy defaultdict: the first write to a key opens a new StreamSink
    via the channel's ``open_stream()``. The sink is closed explicitly via
    ``close()`` or when the whole registry is torn down.
    """

    def __init__(self) -> None:
        self._sinks: dict[SessionKey, Any] = {}
        self._stacks: dict[SessionKey, contextlib.AsyncExitStack] = {}

    async def get_or_open(self, key: SessionKey, channel: Any) -> Any:
        if key not in self._sinks:
            stack = contextlib.AsyncExitStack()
            try:
                sink = await stack.enter_async_context(channel.open_stream(key))
            except Exception:
                await stack.aclose()
                raise
            self._stacks[key] = stack
            self._sinks[key] = sink
        return self._sinks[key]

    async def close(self, key: SessionKey) -> None:
        stack = self._stacks.pop(key, None)
        if stack:
            await stack.aclose()
        self._sinks.pop(key, None)

    async def close_all(self) -> None:
        for key in list(self._stacks):
            await self.close(key)
