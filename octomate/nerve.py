from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, ClassVar, Literal, Protocol, runtime_checkable

import anyio
from anyio.streams.memory import MemoryObjectReceiveStream, MemoryObjectSendStream

from octomate.schemas.conversation import ConversationKey
from octomate.schemas.events import AgentInput
from octomate.schemas.segments import MessageSegment

logger = logging.getLogger(__name__)


SignalKind = Literal[
    "message_batch",
    "send_segments",
    "stream_frame",
]


@dataclass
class Signal:
    """Base for everything that flows on a `Nerve`.

    The full set of valid kinds lives here as `SignalKind`; each subclass
    narrows `kind` to its own specific `Literal[...]`. The dispatcher reads
    `signal.kind` to route to handlers — adding a new signal means extending
    `SignalKind` first, which forces every dispatch site to acknowledge it.
    """

    kind: ClassVar[SignalKind]


@dataclass
class MessageBatch(Signal):
    """Inbound agent input from a channel — fresh user messages and/or a
    `ResumeEvent` for a deferred-tool resolution."""

    kind: ClassVar[Literal["message_batch"]] = "message_batch"
    key: ConversationKey
    events: list[AgentInput]


@dataclass
class SendSegments(Signal):
    """Discrete outbound reply — final message, approval card, etc.

    `target_key` selects the destination channel via its `channel_tentacle_id`;
    the producing agent doesn't need to know what kind of channel it is.
    """

    kind: ClassVar[Literal["send_segments"]] = "send_segments"
    target_key: ConversationKey
    segments: list[MessageSegment]
    reply_to: str | None = None


@dataclass
class StreamFrame(Signal):
    """Incremental streaming output. `frame_type="close"` terminates the sink."""

    kind: ClassVar[Literal["stream_frame"]] = "stream_frame"
    target_key: ConversationKey
    frame_type: Literal["status", "append", "thinking", "flush", "close"]
    payload: dict[str, Any] | None = None


ChannelSignal = MessageBatch
AgentSignal = SendSegments | StreamFrame

DEFAULT_BUFFER = 128


class Nerve[T: Signal]:
    """One-producer-many-consumer typed memory pipe."""

    _send: MemoryObjectSendStream[T]
    _receive: MemoryObjectReceiveStream[T]

    def __init__(self, buffer_size: int = DEFAULT_BUFFER) -> None:
        self._send, self._receive = anyio.create_memory_object_stream[T](
            max_buffer_size=buffer_size
        )

    async def send(self, signal: T) -> None:
        await self._send.send(signal)

    def __aiter__(self) -> AsyncIterator[T]:
        return self._receive.__aiter__()

    async def aclose(self) -> None:
        await self._send.aclose()
        await self._receive.aclose()


Handler = Callable[[Any], Awaitable[None]]
"""A signal handler. Typed against `Any` rather than the pipe's `T` because
the dispatcher routes by `kind` at runtime — a handler registered for one
kind only ever sees that kind's concrete subtype, so callers naturally
declare `async def on_foo(sig: Foo)` and the static checker shouldn't
demand they accept the full union."""


class NerveDispatcher[T: Signal]:
    """Reads from a `Nerve[T]`, fans each signal to handlers keyed on `kind`.

    Handlers run in an anyio TaskGroup so one slow/failing handler doesn't
    block the dispatch loop. Exceptions are logged and isolated per-signal.
    """

    nerve: Nerve[T]
    handlers: dict[SignalKind, list[Handler]]

    def __init__(self, nerve: Nerve[T]) -> None:
        self.nerve = nerve
        self.handlers = {}

    def on(self, kind: SignalKind, handler: Handler) -> None:
        self.handlers.setdefault(kind, []).append(handler)

    async def run(self) -> None:
        async with anyio.create_task_group() as tg:
            async for signal in self.nerve:
                handlers = self.handlers.get(signal.kind, [])
                if not handlers:
                    logger.debug("no handler registered for kind=%s", signal.kind)
                    continue
                for handler in handlers:
                    tg.start_soon(self._invoke, handler, signal)

    @staticmethod
    async def _invoke(handler: Handler, signal: Any) -> None:
        try:
            await handler(signal)
        except Exception:
            logger.exception("handler raised on signal %r", signal)


@runtime_checkable
class StreamSink(Protocol):
    """A channel-specific consumer of `StreamFrame`s for one conversation.

    DevUI's sink is a queue feeding an SSE response. Slack's sink (later)
    will batch frames and edit a single thread message. Each channel
    implements its own; the registry routes frames to the right one.
    """

    async def write(self, frame: StreamFrame) -> None: ...

    async def aclose(self) -> None: ...


SinkFactory = Callable[[], StreamSink]


@dataclass
class SinkRegistry:
    """Lazy `ConversationKey → StreamSink` map.

    `get_or_open(key, factory)` returns the existing sink for `key` or calls
    `factory()` to create one. `set(key, sink)` lets a channel pre-register
    a sink (DevUI does this before kicking, so the sink exists by the time
    the first StreamFrame arrives). `close(key)` terminates and removes.
    """

    _sinks: dict[ConversationKey, StreamSink] = field(default_factory=dict)

    def get_or_open(self, key: ConversationKey, factory: SinkFactory) -> StreamSink:
        sink = self._sinks.get(key)
        if sink is None:
            sink = factory()
            self._sinks[key] = sink
        return sink

    def set(self, key: ConversationKey, sink: StreamSink) -> None:
        self._sinks[key] = sink

    def get(self, key: ConversationKey) -> StreamSink | None:
        return self._sinks.get(key)

    async def close(self, key: ConversationKey) -> None:
        sink = self._sinks.pop(key, None)
        if sink is not None:
            await sink.aclose()

    async def close_all(self) -> None:
        sinks = list(self._sinks.values())
        self._sinks.clear()
        for sink in sinks:
            try:
                await sink.aclose()
            except Exception:
                logger.exception("error closing sink during close_all")
