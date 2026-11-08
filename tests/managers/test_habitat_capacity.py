from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import AsyncExitStack
from unittest.mock import AsyncMock

import pytest
from uuid_utils.compat import uuid7

from octomate.config.habitat import SbxHabitatConfig
from octomate.habitat.sbx import SbxHabitat
from octomate.managers.habitat import HabitatError, HabitatManager
from octomate.nerve import Nerve, UserTopic
from octomate.schemas.habitat import HabitatProgress, InvocationScope
from tests.support.nerve import held
from tests.test_habitat_contracts import IMAGE


@pytest.fixture
async def manager(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[HabitatManager]:
    manager = HabitatManager(SbxHabitatConfig(image=IMAGE, idle_timeout=60), Nerve())

    monkeypatch.setattr(SbxHabitat, "prepare", AsyncMock())
    monkeypatch.setattr(SbxHabitat, "stop", AsyncMock())
    try:
        yield manager
    finally:
        await manager.close()


@pytest.fixture
def owners() -> list[InvocationScope]:
    return [InvocationScope(user_id=uuid7(), conversation_id=uuid7()) for _ in range(8)]


async def test_active_user_habitats_are_not_capped(
    manager: HabitatManager, owners: list[InvocationScope]
) -> None:
    async with AsyncExitStack() as stack:
        states = await asyncio.gather(
            *(
                stack.enter_async_context(manager.enter(scope, uuid7()))
                for scope in owners
            )
        )
        assert len(states) == 8
        assert len({state.habitat.name for state in states}) == 8
        assert all(state.status.phase == "ready" for state in states)
        assert all(len(state.invocations) == 1 for state in states)
        assert all(state.idle is None for state in states)


async def test_new_users_do_not_evict_an_idle_habitat(
    manager: HabitatManager, owners: list[InvocationScope]
) -> None:
    idle, *incoming = owners
    async with manager.enter(idle, uuid7()) as state:
        pass
    timer = state.idle
    assert timer is not None
    async with AsyncExitStack() as stack:
        for scope in incoming:
            await stack.enter_async_context(manager.enter(scope, uuid7()))
        assert state.idle is timer
        assert not timer.cancelled()
        assert state.status.phase == "ready"
        assert manager.status(idle).phase == "ready"


async def test_failed_stop_does_not_block_another_user(
    manager: HabitatManager,
    owners: list[InvocationScope],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first, incoming = owners[:2]
    async with manager.enter(first, uuid7()):
        pass
    with monkeypatch.context() as patch:
        patch.setattr(
            manager.residence(first).habitat,
            "stop",
            AsyncMock(side_effect=HabitatError("stop failed")),
        )
        with pytest.raises(HabitatError, match="stop failed"):
            await manager.stop(manager.residence(first))
        async with manager.enter(incoming, uuid7()) as state:
            assert state.status.phase == "ready"
            assert manager.residence(first).needs_cleanup
            assert manager.status(first).phase == "failed"


async def test_idle_timer_resets_on_reuse_and_stopped_habitat_can_restart(
    manager: HabitatManager, owners: list[InvocationScope]
) -> None:
    manager.config = SbxHabitatConfig(image=IMAGE, idle_timeout=0.01)
    scope = owners[0]
    async with manager.enter(scope, uuid7()) as state:
        pass
    timer = state.idle
    assert timer is not None
    async with manager.nerve.subscribe(UserTopic(scope.user_id)) as events:
        async with manager.enter(scope, uuid7()) as reused:
            assert reused is state
            assert timer.cancelled()
            await asyncio.sleep(0.03)
            assert state.status.phase == "ready"
            assert state.idle is None
            assert held(events) == []
        assert state.idle is not timer
        async with asyncio.timeout(1):
            stopping = (await events.receive()).event
            stopped = (await events.receive()).event
        assert isinstance(stopping, HabitatProgress)
        assert isinstance(stopped, HabitatProgress)
        assert stopping.phase == "stopping"
        assert stopped.phase == "stopped"
    assert not state.needs_cleanup
    async with manager.enter(scope, uuid7()) as restarted:
        assert restarted is state
        assert restarted.status.phase == "ready"


async def test_starting_habitat_does_not_block_another_user(
    manager: HabitatManager,
    owners: list[InvocationScope],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    active, starting, incoming = owners[:3]
    preparing, release = asyncio.Event(), asyncio.Event()

    async def prepare() -> None:
        preparing.set()
        await release.wait()

    monkeypatch.setattr(manager.residence(starting).habitat, "prepare", prepare)
    async with AsyncExitStack() as stack, asyncio.TaskGroup() as tasks:
        busy = await stack.enter_async_context(manager.enter(active, uuid7()))
        pending = tasks.create_task(
            stack.enter_async_context(manager.enter(starting, uuid7()))
        )
        await preparing.wait()
        try:
            ready = await stack.enter_async_context(manager.enter(incoming, uuid7()))
            assert ready.status.phase == "ready"
            assert busy.status.phase == "ready"
            assert not pending.done()
        finally:
            release.set()
        await pending
