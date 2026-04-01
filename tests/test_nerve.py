"""Tests for the Nerve abstraction and AnyioNerve implementation."""

import asyncio

import pytest

from octomate.nerve import (
    AgentPending,
    AnyioNerve,
    AskUser,
    ConfirmResult,
    ConfirmTool,
    DismissPending,
    NerveDispatcher,
    NerveStream,
    PendingRequests,
    ReleaseThread,
    SendSegments,
    SinkRegistry,
    StreamFrame,
    SummonAgent,
    TodoResult,
    TodoUpdate,
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


async def test_send_and_receive():
    nerve = AnyioNerve[str](buffer_size=4)
    await nerve.send("hello")
    result = await nerve.receive()
    assert result == "hello"
    await nerve.close()


async def test_iteration():
    nerve = AnyioNerve[int](buffer_size=8)
    for i in range(3):
        await nerve.send(i)
    await nerve.send_stream.aclose()

    collected = []
    async for item in nerve:
        collected.append(item)
    assert collected == [0, 1, 2]


async def test_multiple_items_ordered():
    nerve = AnyioNerve[str](buffer_size=16)
    items = ["a", "b", "c", "d"]
    for item in items:
        await nerve.send(item)

    received = [await nerve.receive() for _ in items]
    assert received == items
    await nerve.close()


async def test_open_is_noop():
    nerve = AnyioNerve[str](buffer_size=4)
    await nerve.open()
    await nerve.send("x")
    assert await nerve.receive() == "x"
    await nerve.close()


async def test_clone_shares_buffer():
    nerve = AnyioNerve[int](buffer_size=8)
    cloned = nerve.clone()

    await nerve.send(1)
    await nerve.send(2)

    assert await cloned.receive() == 1
    assert await nerve.receive() == 2

    await nerve.close()
    await cloned.close()


async def test_agent_signals_round_trip():
    """All AgentSignal subclasses can be sent and received via AnyioNerve."""
    key = _key()
    nerve = AnyioNerve(buffer_size=32)

    signals = [
        SummonAgent(key=key, agent_tag="claude", contents=[], summary="s"),
        StreamFrame(key=key, frame_type="status", content="ok"),
        StreamFrame(key=key, frame_type="close", content=""),
        AskUser(key=key, request_id="r1", question="q?", options=["a", "b"], multi_select=True),
        ConfirmTool(key=key, request_id="r2", tool_name="t", args={}, title="", description="", skill=""),
        TodoUpdate(key=key, items=[], existing_card_ref="old-ref"),
        DismissPending(key=key, card_ref="ref-123"),
        ReleaseThread(key=key),
        UserAnswer(request_id="r1", answer="yes"),
        ConfirmResult(request_id="r2", approved=True),
        TodoResult(key=key, card_ref="card-abc"),
        SendSegments(key=key, segments=[]),
    ]
    for s in signals:
        await nerve.send(s)

    received = [await nerve.receive() for _ in signals]
    assert [type(r) for r in received] == [type(s) for s in signals]

    ask_user: AskUser = received[3]
    assert ask_user.multi_select is True
    assert ask_user.options == ["a", "b"]
    assert ask_user.request_id == "r1"
    assert ask_user.kind == "ask_user"

    stream_close: StreamFrame = received[2]
    assert stream_close.frame_type == "close"
    assert stream_close.kind == "stream_frame"

    todo_result: TodoResult = received[10]
    assert todo_result.card_ref == "card-abc"

    dismiss: DismissPending = received[6]
    assert dismiss.card_ref == "ref-123"

    todo_update: TodoUpdate = received[5]
    assert todo_update.existing_card_ref == "old-ref"

    await nerve.close()


async def test_signal_discriminators():
    """Each signal has a unique kind discriminator field."""
    key = _key()
    kinds = {
        SummonAgent(key=key, agent_tag="a", contents=[], summary="s").kind,
        StreamFrame(key=key, frame_type="status", content="").kind,
        SendSegments(key=key, segments=[]).kind,
        AskUser(key=key, request_id="r", question="q").kind,
        UserAnswer(request_id="r", answer="a").kind,
        ConfirmTool(key=key, request_id="r", tool_name="t", args={}, title="", description="", skill="").kind,
        ConfirmResult(request_id="r", approved=True).kind,
        TodoUpdate(key=key, items=[]).kind,
        TodoResult(key=key, card_ref="ref").kind,
        DismissPending(key=key).kind,
        ReleaseThread(key=key).kind,
    }
    # All discriminators must be unique
    assert len(kinds) == 11


async def test_pending_requests_create_and_resolve():
    """PendingRequests creates futures and resolves them with values."""
    pending: PendingRequests[str] = PendingRequests()
    future = pending.create("req-1")
    assert not future.done()
    pending.resolve("req-1", "hello")
    assert future.done()
    assert await future == "hello"


async def test_pending_requests_resolve_unknown_is_noop():
    """Resolving an unknown request_id does not raise."""
    pending: PendingRequests[str] = PendingRequests()
    pending.resolve("unknown", "value")  # should not raise


async def test_pending_requests_cancel():
    """PendingRequests.cancel() cancels the future."""
    pending: PendingRequests[int] = PendingRequests()
    future = pending.create("req-cancel")
    pending.cancel("req-cancel")
    assert future.cancelled()


async def test_agent_pending_container():
    """AgentPending groups answers, confirmations, and todos."""
    ap = AgentPending()
    assert isinstance(ap.answers, PendingRequests)
    assert isinstance(ap.confirmations, PendingRequests)
    assert isinstance(ap.todos, PendingRequests)
    # Each is a separate instance
    assert ap.answers is not ap.confirmations
    assert ap.confirmations is not ap.todos


async def test_nerve_stream_sends_frames():
    """NerveStream methods send the correct StreamFrame signals."""
    key = _key()
    nerve = AnyioNerve(buffer_size=16)

    async with NerveStream(nerve, key) as stream:
        await stream.set_status("working")
        await stream.append("hello")
        await stream.flush()
        await stream.post_thinking_block("thinking...")

    frames = [await nerve.receive() for _ in range(5)]
    assert frames[0].frame_type == "status"
    assert frames[0].content == "working"
    assert frames[1].frame_type == "append"
    assert frames[1].content == "hello"
    assert frames[2].frame_type == "flush"
    assert frames[3].frame_type == "thinking"
    assert frames[3].content == "thinking..."
    assert frames[4].frame_type == "close"  # from __aexit__

    await nerve.close()


async def test_nerve_stream_attrs_are_public():
    """NerveStream exposes nerve and key as public attributes."""
    key = _key()
    nerve = AnyioNerve(buffer_size=4)
    stream = NerveStream(nerve, key)
    assert stream.nerve is nerve
    assert stream.key is key
    await nerve.close()


async def test_sink_registry_is_empty_initially():
    """SinkRegistry starts with no open sinks."""
    registry = SinkRegistry()
    assert len(registry._sinks) == 0


async def test_nerve_dispatcher_routes_by_type():
    """NerveDispatcher calls the handler registered for the exact signal type."""
    key = _key()
    nerve = AnyioNerve(buffer_size=8)
    dispatcher = NerveDispatcher(nerve)

    received_stream: list[StreamFrame] = []
    received_ask: list[AskUser] = []

    @dispatcher.on(StreamFrame)
    async def on_stream(sig: StreamFrame) -> None:
        received_stream.append(sig)

    @dispatcher.on(AskUser)
    async def on_ask(sig: AskUser) -> None:
        received_ask.append(sig)

    await nerve.send(StreamFrame(key=key, frame_type="append", content="hi"))
    await nerve.send(AskUser(key=key, request_id="r", question="?"))
    await nerve.send(StreamFrame(key=key, frame_type="flush", content=""))
    await nerve.send_stream.aclose()

    await dispatcher.run()

    assert len(received_stream) == 2
    assert received_stream[0].frame_type == "append"
    assert received_stream[1].frame_type == "flush"
    assert len(received_ask) == 1
    assert received_ask[0].request_id == "r"


async def test_octopus_has_agent_nerve():
    """Octopus exposes agent_nerve, agent_dispatcher, AgentPending and SinkRegistry."""
    from octomate.octopus import Octopus

    octopus = Octopus()
    assert octopus.agent_nerve is not None
    assert octopus.agent_dispatcher is not None
    assert isinstance(octopus.pending, AgentPending)
    assert isinstance(octopus.sinks, SinkRegistry)
