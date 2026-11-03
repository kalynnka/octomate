from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from unittest.mock import AsyncMock

import anyio
import pytest
from anyio.abc import ByteStream
from pydantic import UUID7
from uuid_utils.compat import uuid7

import octomate.sandbox.agent as agent_module
from octomate.capabilities.harness.events import RunEndedEvent
from octomate.config.sandbox import SbxSandboxConfig
from octomate.managers.sandbox import SandboxManager
from octomate.nerve import (
    Impulse,
    Nerve,
    NerveEvent,
    SandboxTopic,
    impulse_adapter,
)
from octomate.sandbox import SandboxError
from octomate.sandbox.agent import run_agent
from octomate.schemas.sandbox import (
    CancelInvocation,
    InvocationScope,
    SandboxAgentReady,
)
from tests.support.nerve import held
from tests.test_sandbox_contracts import IMAGE


@dataclass
class GuestSandbox:
    manager: SandboxManager
    scope: InvocationScope
    ready: SandboxAgentReady
    name: str = "test-sandbox"
    fault: bytes | None = None
    silent: bool = False
    calls: list[str] = field(default_factory=list)
    opened: asyncio.Event = field(default_factory=asyncio.Event)
    inputs: dict[UUID7, asyncio.Queue[bytes | None]] = field(default_factory=dict)

    async def prepare(self) -> None:
        self.calls.append("prepare")

    async def stop(self) -> None:
        self.calls.append("stop")

    @asynccontextmanager
    async def execute(
        self, *command: str, workdir: str, env: dict[str, str] | None = None
    ) -> AsyncGenerator[ByteStream]:
        assert command == (
            "/opt/octomate/.venv/bin/python",
            "-m",
            "octomate.sandbox.entrypoint",
        )
        assert workdir == "/home/agent/workspace"
        assert env is None
        state = self.manager.state(self.scope.user_id)
        (invocation,) = state.invocations - self.inputs.keys()
        assert invocation in state.invocations
        assert state.snapshot.phase == "ready"
        self.calls.append("agent:start")
        incoming: asyncio.Queue[bytes | None] = asyncio.Queue()
        self.inputs[invocation] = incoming
        if not self.silent:
            ready = Impulse(self.ready)
            incoming.put_nowait(
                self.fault
                if self.fault is not None
                else impulse_adapter.dump_json(ready) + b"\n"
            )

        async def receive() -> bytes:
            frame = await incoming.get()
            if frame is None:
                raise anyio.EndOfStream
            return frame

        stream = AsyncMock(spec=ByteStream)
        stream.receive.side_effect = receive
        stream.send.side_effect = AssertionError(
            "The event fiber must never write to the guest"
        )
        self.opened.set()
        try:
            yield stream
        finally:
            assert invocation in state.invocations
            stream.aclose.assert_awaited()
            stream.send.assert_not_awaited()
            self.calls.append("agent:stop")


@pytest.fixture
async def guest() -> AsyncGenerator[GuestSandbox]:
    manager = SandboxManager(SbxSandboxConfig(image=IMAGE, idle_timeout=60), Nerve())
    scope = InvocationScope(user_id=uuid7(), mode="sbx", conversation_id=uuid7())
    guest = GuestSandbox(
        manager,
        scope,
        SandboxAgentReady(
            user_id=scope.user_id,
            image=IMAGE,
            cpus=2,
            memory_mib=2048,
            accepts_invocations=False,
        ),
    )
    manager.state(scope.user_id).sandbox = guest
    try:
        yield guest
    finally:
        await manager.close()


async def test_acquire_run_release_keeps_vm_ready_until_idle_stop(
    guest: GuestSandbox,
) -> None:
    manager, scope, invocation = guest.manager, guest.scope, uuid7()
    topic = SandboxTopic(scope.user_id, invocation)
    async with manager.nerve.subscribe(topic) as events:
        async with run_agent(manager, scope, invocation) as fiber:
            assert (await events.receive()).event == guest.ready
            assert not fiber.closed
            assert guest.calls == ["prepare", "agent:start"]
            assert manager.state(scope.user_id).idle is None
        assert fiber.closed
        assert held(events) == []
    state = manager.state(scope.user_id)
    assert not state.invocations
    assert state.idle is not None
    assert state.snapshot.phase == "ready"
    assert guest.calls == ["prepare", "agent:start", "agent:stop"]
    assert not manager.nerve.subscribers
    await manager.stop(scope.user_id)
    assert guest.calls[-1] == "stop"


@pytest.mark.parametrize(
    "fault",
    ["wrong-owner", "wrong-image", "bad-json", "missing-event", "unexpected-event"],
)
async def test_agent_readiness_failure_releases_vm_without_poisoning_it(
    guest: GuestSandbox, fault: str
) -> None:
    invocation = uuid7()
    if fault == "wrong-owner":
        guest.ready.user_id = uuid7()
    elif fault == "wrong-image":
        guest.ready.image = "different"
    else:
        impulse: Impulse[NerveEvent] = Impulse(guest.ready)
        if fault == "unexpected-event":
            impulse = Impulse(CancelInvocation(invocation_id=invocation))
        wire = impulse_adapter.dump_json(impulse)
        if fault == "missing-event":
            wire = b"{}"
        if fault == "bad-json":
            wire = b"secret=never-publish-this"
        guest.fault = wire + b"\n"
    with pytest.raises(SandboxError, match="readiness") as failure:
        async with run_agent(guest.manager, guest.scope, invocation):
            pytest.fail("invalid readiness was accepted")
    assert "never-publish" not in str(failure.value)
    assert guest.calls[-1] == "agent:stop"
    assert not guest.manager.nerve.subscribers
    state = guest.manager.state(guest.scope.user_id)
    assert not state.invocations
    async with guest.manager.acquire(guest.scope, uuid7()) as reused:
        assert reused is state
        assert reused.snapshot.phase == "ready"


@pytest.mark.parametrize("cancel", [False, True])
async def test_interrupted_agent_startup_cleans_up_before_release(
    guest: GuestSandbox, monkeypatch: pytest.MonkeyPatch, cancel: bool
) -> None:
    guest.silent = True
    monkeypatch.setattr(agent_module, "READY_TIMEOUT", 60 if cancel else 0.01)

    async def run() -> None:
        async with run_agent(guest.manager, guest.scope, uuid7()):
            pytest.fail("agent became ready without a handshake")

    task = asyncio.create_task(run())
    await guest.opened.wait()
    if cancel:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    else:
        with pytest.raises(SandboxError, match="readiness"):
            await task
    assert guest.calls[-1] == "agent:stop"
    assert not guest.manager.state(guest.scope.user_id).invocations
    assert not guest.manager.nerve.subscribers


async def test_same_user_runs_share_vm_but_have_separate_fibers(
    guest: GuestSandbox,
) -> None:
    manager, scope = guest.manager, guest.scope
    first, second = uuid7(), uuid7()
    topic = SandboxTopic(scope.user_id, first)
    async with (
        run_agent(manager, scope, first) as active,
        manager.nerve.subscribe(topic) as events,
    ):
        async with (
            run_agent(manager, scope, second) as peer,
            manager.nerve.subscribe(
                SandboxTopic(scope.user_id, second)
            ) as other_events,
        ):
            assert active is not peer
            assert len(manager.state(scope.user_id).invocations) == 2
            local = Impulse(CancelInvocation(invocation_id=first))
            manager.nerve.deliver(topic, local)
            assert await events.receive() == local
            impulse = Impulse(RunEndedEvent(), invocation_id=first)
            await guest.inputs[first].put(impulse_adapter.dump_json(impulse) + b"\n")
            assert await asyncio.wait_for(events.receive(), 1) == impulse
            assert held(other_events) == []
        assert peer.closed
        assert not active.closed
        assert manager.state(scope.user_id).invocations == {first}
        await guest.inputs[first].put(impulse_adapter.dump_json(impulse) + b"\n")
        assert await asyncio.wait_for(events.receive(), 1) == impulse
    assert guest.calls.count("prepare") == 1
    assert guest.calls.count("agent:stop") == 2


async def test_run_failure_releases_usage_and_keeps_vm_reusable(
    guest: GuestSandbox,
) -> None:
    with pytest.raises(RuntimeError, match="run failed"):
        async with run_agent(guest.manager, guest.scope, uuid7()) as fiber:
            raise RuntimeError("run failed")
    assert fiber.closed
    assert not guest.manager.state(guest.scope.user_id).invocations
    async with run_agent(guest.manager, guest.scope, uuid7()):
        assert guest.calls.count("prepare") == 1
    assert guest.calls.count("agent:stop") == 2
