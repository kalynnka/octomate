"""Cancellation utilities preserve resource ownership across late completion."""

import asyncio
import logging
import threading
from contextvars import ContextVar
from unittest.mock import Mock

import anyio
import pytest

from octomate.utils import acquire_in_thread, drain_task


async def test_drain_task_defers_repeated_cancellation() -> None:
    release = asyncio.Event()
    worker = asyncio.create_task(release.wait())
    draining = asyncio.create_task(drain_task(worker))
    await asyncio.sleep(0)
    try:
        for _ in range(2):
            draining.cancel()
            await asyncio.sleep(0)
            assert not draining.done()
            assert not worker.cancelled()
    finally:
        release.set()
    assert await draining is True
    assert worker.result() is True


async def test_drain_task_shields_an_anyio_cancel_scope() -> None:
    future: asyncio.Future[int] = asyncio.get_running_loop().create_future()
    asyncio.get_running_loop().call_soon(future.set_result, 7)
    with anyio.CancelScope() as scope:
        scope.cancel()
        assert await drain_task(future) is False
    assert future.result() == 7


@pytest.mark.parametrize("done", [False, True])
@pytest.mark.parametrize("cancelled", [False, True])
async def test_drain_task_propagates_worker_failure(
    done: bool, cancelled: bool
) -> None:
    loop = asyncio.get_running_loop()
    future: asyncio.Future[int] = loop.create_future()

    def fail() -> None:
        if cancelled:
            future.cancel()
        else:
            future.set_exception(ValueError("worker failed"))

    if done:
        fail()
    else:
        loop.call_soon(fail)
    with pytest.raises(asyncio.CancelledError if cancelled else ValueError):
        await drain_task(future)


async def test_thread_result_keeps_context_and_belongs_to_the_caller() -> None:
    context = ContextVar("test_context", default="missing")
    token = context.set("request context")
    discard = Mock()
    try:
        assert (
            await acquire_in_thread(context.get, discard=discard) == "request context"
        )
    finally:
        context.reset(token)
    discard.assert_not_called()


async def test_cancelled_thread_returns_immediately_and_disposes_on_event_loop() -> (
    None
):
    loop = asyncio.get_running_loop()
    started, discarded = asyncio.Event(), asyncio.Event()
    release = threading.Event()
    disposed: list[tuple[str, int]] = []

    def work() -> str:
        loop.call_soon_threadsafe(started.set)
        if not release.wait(timeout=5):
            raise TimeoutError("test did not release worker")
        return "resource"

    def discard(value: str) -> None:
        disposed.append((value, threading.get_ident()))
        discarded.set()

    caller = asyncio.create_task(acquire_in_thread(work, discard=discard))
    try:
        await asyncio.wait_for(started.wait(), 1)
        caller.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(caller, 1)
        assert not discarded.is_set()
    finally:
        release.set()
    await asyncio.wait_for(discarded.wait(), 1)
    assert disposed == [("resource", threading.get_ident())]


@pytest.mark.parametrize("failed", [False, True])
async def test_thread_completion_racing_cancellation_is_handled_once(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, failed: bool
) -> None:
    loop = asyncio.get_running_loop()
    future: asyncio.Future[str] = loop.create_future()
    monkeypatch.setattr(loop, "run_in_executor", lambda *args: future)
    discard = Mock()
    caplog.set_level(logging.DEBUG, logger="octomate.utils")
    caller = asyncio.create_task(acquire_in_thread(lambda: "resource", discard=discard))
    await asyncio.sleep(0)
    if failed:
        future.set_exception(ValueError("late worker failure"))
    else:
        future.set_result("resource")
    caller.cancel()
    with pytest.raises(asyncio.CancelledError):
        await caller
    await asyncio.sleep(0)
    if failed:
        discard.assert_not_called()
        assert "late worker failure" in caplog.text
    else:
        discard.assert_called_once_with("resource")


async def test_thread_failure_reaches_an_attached_caller() -> None:
    def fail() -> str:
        raise ValueError("worker failed")

    discard = Mock()
    with pytest.raises(ValueError, match="worker failed"):
        await acquire_in_thread(fail, discard=discard)
    discard.assert_not_called()
