from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import AsyncExitStack
from unittest.mock import AsyncMock

import pytest
from uuid_utils.compat import uuid7

from octomate.config.sandbox import SbxSandboxConfig
from octomate.managers.sandbox import SandboxError, SandboxManager
from octomate.nerve import Nerve, UserTopic
from octomate.sandbox.sbx import SbxSandbox
from octomate.schemas.sandbox import InvocationScope, SandboxProgress
from tests.support.nerve import held
from tests.test_sandbox_contracts import IMAGE


@pytest.fixture
async def manager(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[SandboxManager]:
    manager = SandboxManager(SbxSandboxConfig(image=IMAGE, idle_timeout=60), Nerve())

    monkeypatch.setattr(SbxSandbox, "prepare", AsyncMock())
    monkeypatch.setattr(SbxSandbox, "stop", AsyncMock())
    try:
        yield manager
    finally:
        await manager.close()


@pytest.fixture
def owners() -> list[InvocationScope]:
    return [
        InvocationScope(user_id=uuid7(), mode="sbx", conversation_id=uuid7())
        for _ in range(8)
    ]


async def test_active_user_sandboxes_are_not_capped(
    manager: SandboxManager, owners: list[InvocationScope]
) -> None:
    async with AsyncExitStack() as stack:
        states = await asyncio.gather(
            *(
                stack.enter_async_context(manager.acquire(scope, uuid7()))
                for scope in owners
            )
        )
        assert len(states) == 8
        assert len({state.sandbox.name for state in states}) == 8
        assert all(state.snapshot.phase == "ready" for state in states)
        assert all(len(state.invocations) == 1 for state in states)
        assert all(state.idle is None for state in states)


async def test_new_users_do_not_evict_an_idle_sandbox(
    manager: SandboxManager, owners: list[InvocationScope]
) -> None:
    idle, *incoming = owners
    async with manager.acquire(idle, uuid7()) as state:
        pass
    timer = state.idle
    assert timer is not None
    async with AsyncExitStack() as stack:
        for scope in incoming:
            await stack.enter_async_context(manager.acquire(scope, uuid7()))
        assert state.idle is timer
        assert not timer.cancelled()
        assert state.snapshot.phase == "ready"
        assert manager.snapshot(idle).phase == "ready"


async def test_failed_stop_does_not_block_another_user(
    manager: SandboxManager,
    owners: list[InvocationScope],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first, incoming = owners[:2]
    async with manager.acquire(first, uuid7()):
        pass
    with monkeypatch.context() as patch:
        patch.setattr(
            manager.state(first.user_id).sandbox,
            "stop",
            AsyncMock(side_effect=SandboxError("stop failed")),
        )
        with pytest.raises(SandboxError, match="stop failed"):
            await manager.stop(first.user_id)
        async with manager.acquire(incoming, uuid7()) as state:
            assert state.snapshot.phase == "ready"
            assert manager.state(first.user_id).needs_cleanup
            assert manager.snapshot(first).phase == "failed"


async def test_idle_timer_resets_on_reuse_and_stopped_sandbox_can_restart(
    manager: SandboxManager, owners: list[InvocationScope]
) -> None:
    manager.config = SbxSandboxConfig(image=IMAGE, idle_timeout=0.01)
    scope = owners[0]
    async with manager.acquire(scope, uuid7()) as state:
        pass
    timer = state.idle
    assert timer is not None
    async with manager.nerve.subscribe(UserTopic(scope.user_id)) as events:
        async with manager.acquire(scope, uuid7()) as reused:
            assert reused is state
            assert timer.cancelled()
            await asyncio.sleep(0.03)
            assert state.snapshot.phase == "ready"
            assert state.idle is None
            assert held(events) == []
        assert state.idle is not timer
        async with asyncio.timeout(1):
            stopping = (await events.receive()).event
            stopped = (await events.receive()).event
        assert isinstance(stopping, SandboxProgress)
        assert isinstance(stopped, SandboxProgress)
        assert stopping.phase == "stopping"
        assert stopped.phase == "stopped"
    assert not state.needs_cleanup
    async with manager.acquire(scope, uuid7()) as restarted:
        assert restarted is state
        assert restarted.snapshot.phase == "ready"


async def test_starting_sandbox_does_not_block_another_user(
    manager: SandboxManager,
    owners: list[InvocationScope],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    active, starting, incoming = owners[:3]
    preparing, release = asyncio.Event(), asyncio.Event()

    async def prepare() -> None:
        preparing.set()
        await release.wait()

    monkeypatch.setattr(manager.state(starting.user_id).sandbox, "prepare", prepare)
    async with AsyncExitStack() as stack, asyncio.TaskGroup() as tasks:
        busy = await stack.enter_async_context(manager.acquire(active, uuid7()))
        pending = tasks.create_task(
            stack.enter_async_context(manager.acquire(starting, uuid7()))
        )
        await preparing.wait()
        try:
            ready = await stack.enter_async_context(manager.acquire(incoming, uuid7()))
            assert ready.snapshot.phase == "ready"
            assert busy.snapshot.phase == "ready"
            assert not pending.done()
        finally:
            release.set()
        await pending
