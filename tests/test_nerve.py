"""Tests for the Nerve abstraction and AnyioNerve implementation."""

import pytest

from octomate.nerve import AnyioNerve


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
