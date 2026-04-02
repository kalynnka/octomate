"""Tests for NerveDispatcher: routing, error isolation, and concurrent dispatch."""

import asyncio

from octomate.nerve import (
    AnyioNerve,
    AskUser,
    NerveDispatcher,
    SendSegments,
    StreamFrame,
    UserAnswer,
)
from octomate.schemas.session import SessionKey


def _key(user_id: str = "u1") -> SessionKey:
    return SessionKey(
        tentacle_id="t1",
        user_id=user_id,
        group_id="",
        thread_id="",
        chat_id=user_id,
    )


async def test_register_and_call_handler():
    """register() adds a handler that is invoked for matching signals."""
    key = _key()
    nerve = AnyioNerve(buffer_size=4)
    dispatcher = NerveDispatcher(nerve)

    received: list[AskUser] = []

    async def on_ask(sig: AskUser) -> None:
        received.append(sig)

    dispatcher.register(AskUser, on_ask)
    await nerve.send(AskUser(key=key, request_id="r1", question="q?"))
    await nerve.send_stream.aclose()

    await dispatcher.run()

    assert len(received) == 1
    assert received[0].request_id == "r1"


async def test_unregistered_signal_type_is_silently_ignored():
    """Signals with no registered handlers are dropped without error."""
    key = _key()
    nerve = AnyioNerve(buffer_size=4)
    dispatcher = NerveDispatcher(nerve)

    # No handlers registered — sending a signal should not raise
    await nerve.send(StreamFrame(key=key, frame_type="append", content="hi"))
    await nerve.send_stream.aclose()

    await dispatcher.run()  # should complete cleanly


async def test_multiple_handlers_for_same_type():
    """Multiple handlers registered for the same type are all invoked."""
    key = _key()
    nerve = AnyioNerve(buffer_size=4)
    dispatcher = NerveDispatcher(nerve)

    calls: list[str] = []

    @dispatcher.on(UserAnswer)
    async def handler_a(sig: UserAnswer) -> None:
        calls.append("a")

    @dispatcher.on(UserAnswer)
    async def handler_b(sig: UserAnswer) -> None:
        calls.append("b")

    await nerve.send(UserAnswer(key=key, request_id="r", answer="yes"))
    await nerve.send_stream.aclose()

    await dispatcher.run()

    assert sorted(calls) == ["a", "b"]


async def test_handler_error_does_not_crash_dispatcher():
    """A handler raising an exception is logged but does not stop the dispatcher."""
    key = _key()
    nerve = AnyioNerve(buffer_size=8)
    dispatcher = NerveDispatcher(nerve)

    processed: list[str] = []

    @dispatcher.on(AskUser)
    async def bad_handler(sig: AskUser) -> None:
        raise RuntimeError("boom")

    @dispatcher.on(SendSegments)
    async def good_handler(sig: SendSegments) -> None:
        processed.append("ok")

    await nerve.send(AskUser(key=key, request_id="r1", question="q?"))
    await nerve.send(SendSegments(key=key, segments=[]))
    await nerve.send_stream.aclose()

    await dispatcher.run()

    assert processed == ["ok"]


async def test_concurrent_dispatch_all_handlers_run():
    """Multiple signals are dispatched concurrently; all handlers complete."""
    key = _key()
    nerve = AnyioNerve(buffer_size=16)
    dispatcher = NerveDispatcher(nerve)

    results: list[int] = []

    @dispatcher.on(StreamFrame)
    async def on_frame(sig: StreamFrame) -> None:
        await asyncio.sleep(0)
        results.append(int(sig.content))

    for i in range(5):
        await nerve.send(StreamFrame(key=key, frame_type="append", content=str(i)))
    await nerve.send_stream.aclose()

    await dispatcher.run()

    assert sorted(results) == [0, 1, 2, 3, 4]


async def test_on_decorator_returns_handler():
    """The @dispatcher.on() decorator returns the original handler unchanged."""
    nerve = AnyioNerve(buffer_size=4)
    dispatcher = NerveDispatcher(nerve)

    async def my_handler(sig: AskUser) -> None:
        pass

    result = dispatcher.on(AskUser)(my_handler)
    assert result is my_handler
    await nerve.close()


async def test_dispatcher_exits_when_nerve_send_stream_closed():
    """Closing the send stream causes run() to return without cancellation."""
    key = _key()
    nerve = AnyioNerve(buffer_size=4)
    dispatcher = NerveDispatcher(nerve)

    received: list[StreamFrame] = []

    @dispatcher.on(StreamFrame)
    async def on_frame(sig: StreamFrame) -> None:
        received.append(sig)

    await nerve.send(StreamFrame(key=key, frame_type="status", content="ready"))
    await nerve.send_stream.aclose()

    await dispatcher.run()

    assert len(received) == 1
    assert received[0].content == "ready"


async def test_signal_routing_does_not_cross_types():
    """Handlers for type A are never called for type B signals."""
    key = _key()
    nerve = AnyioNerve(buffer_size=8)
    dispatcher = NerveDispatcher(nerve)

    ask_calls: list = []
    stream_calls: list = []

    @dispatcher.on(AskUser)
    async def on_ask(sig: AskUser) -> None:
        ask_calls.append(sig)

    @dispatcher.on(StreamFrame)
    async def on_stream(sig: StreamFrame) -> None:
        stream_calls.append(sig)

    await nerve.send(AskUser(key=key, request_id="r", question="?"))
    await nerve.send(StreamFrame(key=key, frame_type="flush", content=""))
    await nerve.send(AskUser(key=key, request_id="r2", question="??"))
    await nerve.send_stream.aclose()

    await dispatcher.run()

    assert len(ask_calls) == 2
    assert len(stream_calls) == 1
    assert all(isinstance(s, AskUser) for s in ask_calls)
    assert all(isinstance(s, StreamFrame) for s in stream_calls)
