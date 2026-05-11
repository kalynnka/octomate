"""Unit tests for `octomate.nerve` — pipe dispatch + sink registry."""

from __future__ import annotations

import anyio

from octomate.nerve import (
    MessageBatch,
    Nerve,
    NerveDispatcher,
    SinkRegistry,
    StreamFrame,
)
from octomate.schemas.conversation import ConversationKey


def _key(chat_id: str = "alice") -> ConversationKey:
    return ConversationKey(
        channel_tentacle_id="t",
        chat_type="private",
        chat_id=chat_id,
        user_id="u",
        thread_id="",
    )


async def test_dispatcher_fans_out_by_kind() -> None:
    """Each signal is delivered only to handlers registered for its `kind`."""
    nerve: Nerve = Nerve()
    dispatcher: NerveDispatcher = NerveDispatcher(nerve)

    batches: list[str] = []
    frames: list[str] = []

    async def on_batch(sig: MessageBatch) -> None:
        batches.append(sig.key.chat_id)

    async def on_frame(sig: StreamFrame) -> None:
        frames.append(sig.target_key.chat_id)

    dispatcher.on("message_batch", on_batch)
    dispatcher.on("stream_frame", on_frame)

    async with anyio.create_task_group() as tg:
        tg.start_soon(dispatcher.run)
        await nerve.send(MessageBatch(key=_key("a"), events=[]))
        await nerve.send(StreamFrame(target_key=_key("b"), frame_type="append"))
        await nerve.send(MessageBatch(key=_key("c"), events=[]))
        await anyio.sleep(0.05)
        tg.cancel_scope.cancel()

    assert batches == ["a", "c"]
    assert frames == ["b"]


async def test_dispatcher_isolates_handler_exceptions() -> None:
    """A crashing handler doesn't kill the loop or sibling handlers."""
    nerve: Nerve = Nerve()
    dispatcher: NerveDispatcher = NerveDispatcher(nerve)
    seen: list[str] = []

    async def crashing(_sig: MessageBatch) -> None:
        raise RuntimeError("boom")

    async def collecting(sig: MessageBatch) -> None:
        seen.append(sig.key.chat_id)

    dispatcher.on("message_batch", crashing)
    dispatcher.on("message_batch", collecting)

    async with anyio.create_task_group() as tg:
        tg.start_soon(dispatcher.run)
        await nerve.send(MessageBatch(key=_key("a"), events=[]))
        await nerve.send(MessageBatch(key=_key("b"), events=[]))
        await anyio.sleep(0.05)
        tg.cancel_scope.cancel()

    assert seen == ["a", "b"]


async def test_sink_registry_get_or_open_caches_per_key() -> None:
    """Repeated `get_or_open(key)` returns the same sink without re-invoking factory."""
    registry = SinkRegistry()
    calls = 0

    class FakeSink:
        async def write(self, frame: StreamFrame) -> None: ...

        async def aclose(self) -> None: ...

    def factory() -> FakeSink:
        nonlocal calls
        calls += 1
        return FakeSink()

    key = _key()
    a = registry.get_or_open(key, factory)
    b = registry.get_or_open(key, factory)
    assert a is b
    assert calls == 1


async def test_sink_registry_set_then_close() -> None:
    """`set(key, sink)` overrides the lazy path; `close(key)` calls aclose + evicts."""
    registry = SinkRegistry()
    closed = False

    class FakeSink:
        async def write(self, frame: StreamFrame) -> None: ...

        async def aclose(self) -> None:
            nonlocal closed
            closed = True

    sink = FakeSink()
    key = _key()
    registry.set(key, sink)
    assert registry.get(key) is sink

    await registry.close(key)
    assert closed is True
    assert registry.get(key) is None
