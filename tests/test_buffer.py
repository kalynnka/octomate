"""Unit tests for MessageBuffer: batching, flush delay, session isolation."""
import asyncio

import pytest

from octomate.tentacles.base import MessageBuffer
from tests.helpers import make_group_event, make_private_event


async def test_push_calls_handler_after_flush():
    batches: list = []

    async def handler(key, batch):
        batches.append((key, list(batch)))

    buf = MessageBuffer(flush_delay=0, handler=handler)
    event = make_private_event(user_id="alice")
    event.tentacle_id = "t1"

    buf.push(event)
    await asyncio.sleep(0.02)

    assert len(batches) == 1
    assert batches[0][0] == event.session_key
    assert batches[0][1] == [event]


async def test_rapid_pushes_batched_together():
    """Multiple events for the same session pushed before the flush window
    should arrive as one batch."""
    batches: list = []

    async def handler(key, batch):
        batches.append((key, list(batch)))

    buf = MessageBuffer(flush_delay=0.05, handler=handler)
    e1 = make_private_event(user_id="alice", text="first")
    e2 = make_private_event(user_id="alice", text="second")
    e1.tentacle_id = "t1"
    e2.tentacle_id = "t1"

    buf.push(e1)
    buf.push(e2)

    await asyncio.sleep(0.15)

    assert len(batches) == 1
    assert len(batches[0][1]) == 2
    assert batches[0][1][0].raw_message == "first"
    assert batches[0][1][1].raw_message == "second"


async def test_different_sessions_get_separate_batches():
    batches: list = []

    async def handler(key, batch):
        batches.append((key, list(batch)))

    buf = MessageBuffer(flush_delay=0, handler=handler)
    e_alice = make_private_event(user_id="alice")
    e_bob = make_private_event(user_id="bob")
    e_alice.tentacle_id = "t1"
    e_bob.tentacle_id = "t1"

    buf.push(e_alice)
    buf.push(e_bob)

    await asyncio.sleep(0.02)

    assert len(batches) == 2
    keys = {b[0] for b in batches}
    assert e_alice.session_key in keys
    assert e_bob.session_key in keys


async def test_group_and_private_same_user_are_separate_sessions():
    batches: list = []

    async def handler(key, batch):
        batches.append((key, list(batch)))

    buf = MessageBuffer(flush_delay=0, handler=handler)
    prv = make_private_event(user_id="alice")
    grp = make_group_event(user_id="alice", group_id="g1")
    prv.tentacle_id = "t1"
    grp.tentacle_id = "t1"

    buf.push(prv)
    buf.push(grp)

    await asyncio.sleep(0.02)

    assert len(batches) == 2


async def test_handler_exception_does_not_crash_buffer():
    calls: list[int] = []

    async def flaky_handler(key, batch):
        calls.append(1)
        raise RuntimeError("boom")

    buf = MessageBuffer(flush_delay=0, handler=flaky_handler)
    e = make_private_event()
    e.tentacle_id = "t1"
    buf.push(e)

    await asyncio.sleep(0.02)
    assert calls == [1]
