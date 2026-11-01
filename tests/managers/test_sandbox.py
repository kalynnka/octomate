from __future__ import annotations

import asyncio
from collections.abc import Generator
from dataclasses import dataclass, field
from typing import Unpack
from unittest.mock import AsyncMock

import anyio
import pytest
from anyio.abc import ByteStream
from uuid_utils.compat import uuid7

import octomate.sandbox.sbx as sandbox_module
from octomate.config.sandbox import SbxSandboxConfig
from octomate.managers.sandbox import SandboxError, SandboxManager
from octomate.nerve import (
    Impulse,
    Nerve,
    Publication,
    SandboxTopic,
    UserTopic,
    publication_adapter,
)
from octomate.sandbox.sbx import SbxOptions, SbxSandbox
from octomate.schemas.sandbox import (
    CancelInvocation,
    InvocationScope,
    SandboxAgentReady,
    SandboxFailed,
    SandboxProgress,
)
from tests.support.nerve import held
from tests.test_sandbox_contracts import IMAGE


@dataclass
class GuestProcess:
    stream: AsyncMock = field(default_factory=lambda: AsyncMock(spec=ByteStream))
    handshake: bytes | None = None
    returncode: int | None = None

    def __post_init__(self) -> None:
        self.stream.receive.side_effect = self.receive

    async def receive(self) -> bytes:
        if self.handshake is None:
            return await asyncio.Future[bytes]()
        frame, self.handshake = self.handshake, None
        return frame

    def is_alive(self) -> bool:
        return self.returncode is None

    def __await__(self) -> Generator[None, None, int]:
        return self.wait().__await__()

    def terminate(self) -> None:
        self.returncode = -15

    def kill(self) -> None:
        self.returncode = -9

    async def wait(self) -> int:
        assert self.returncode is not None
        return self.returncode


@pytest.fixture
def scope() -> InvocationScope:
    return InvocationScope(user_id=uuid7(), mode="sbx", conversation_id=uuid7())


@pytest.fixture
def guest(scope: InvocationScope) -> GuestProcess:
    process = GuestProcess()
    process.handshake = (
        Publication(
            topic=SandboxTopic(scope.user_id),
            impulse=Impulse(
                SandboxAgentReady(
                    user_id=scope.user_id,
                    image=IMAGE,
                    cpus=2,
                    memory_mib=2048,
                    accepts_invocations=False,
                )
            ),
        )
        .model_dump_json()
        .encode()
        + b"\n"
    )
    return process


@pytest.fixture
def manager(monkeypatch: pytest.MonkeyPatch, guest: GuestProcess) -> SandboxManager:
    manager = SandboxManager(SbxSandboxConfig(image=IMAGE, idle_timeout=0.01), Nerve())
    monkeypatch.setattr(SbxSandbox, "preflight", AsyncMock())
    names: set[str] = set()

    async def command(*args: str, **options: Unpack[SbxOptions]) -> bytes:
        if args[0] == "create":
            assert "name" in options
            names.add(options["name"])
        return "\n".join(sorted(names)).encode() if args[0] == "ls" else b""

    monkeypatch.setattr(SbxSandbox, "command", AsyncMock(side_effect=command))

    async def open(sandbox: SbxSandbox) -> ByteStream:
        monkeypatch.setattr(sandbox, "process", guest)
        return guest.stream

    monkeypatch.setattr(SbxSandbox, "open", open)
    return manager


async def test_concurrent_acquisitions_share_one_guest_and_keep_peer_alive(
    manager: SandboxManager,
    scope: InvocationScope,
    guest: GuestProcess,
) -> None:
    entered = asyncio.Event()
    release = asyncio.Event()

    async def peer() -> None:
        async with manager.acquire(scope, uuid7()):
            entered.set()
            await release.wait()

    task = asyncio.create_task(peer())
    await entered.wait()
    async with manager.nerve.subscribe(UserTopic(scope.user_id)) as events:
        async with manager.acquire(scope, uuid7()) as state:
            assert isinstance(state.sandbox, SbxSandbox)
            assert state.sandbox.process is guest
            assert state.fiber in manager.nerve.subscribers[SandboxTopic(scope.user_id)]
            assert len(state.invocations) == 2
        await manager.stop(scope.user_id)
        assert guest.returncode is None
        assert held(events) == []
    release.set()
    await task
    await manager.close()
    assert manager.snapshot(scope).phase == "stopped"
    assert not manager.state(scope.user_id).needs_cleanup
    assert not manager.nerve.subscribers


async def test_connection_routes_remain_open_after_prepare_until_stop(
    manager: SandboxManager, scope: InvocationScope, guest: GuestProcess
) -> None:
    sent = asyncio.Future[bytes]()

    async def send(frame: bytes) -> None:
        sent.set_result(frame)

    guest.stream.send.side_effect = send
    topic = SandboxTopic(scope.user_id)
    impulse = Impulse(CancelInvocation(invocation_id=uuid7()))
    async with manager.acquire(scope, uuid7()) as state:
        connection = state.connection
        assert connection is not None
        assert not connection.done()
        manager.nerve.deliver(topic, impulse)
        frame = await asyncio.wait_for(sent, 1)
        assert publication_adapter.validate_json(frame) == Publication(
            topic=topic, impulse=impulse
        )
    await manager.stop(scope.user_id)
    assert connection.done()
    assert state.connection is None
    assert topic not in manager.nerve.subscribers
    guest.stream.aclose.assert_awaited()


async def test_guest_eof_removes_routes_and_prevents_reuse(
    manager: SandboxManager, scope: InvocationScope, guest: GuestProcess
) -> None:
    close = asyncio.Event()

    async def receive() -> bytes:
        if guest.handshake is not None:
            return await guest.receive()
        await close.wait()
        raise anyio.EndOfStream

    guest.stream.receive.side_effect = receive
    async with manager.acquire(scope, uuid7()) as state:
        assert state.connection is not None
        close.set()
        await asyncio.wait_for(asyncio.shield(state.connection), 1)
        assert SandboxTopic(scope.user_id) not in manager.nerve.subscribers
        with pytest.raises(SandboxError, match="connection ended"):
            async with manager.acquire(scope, uuid7()):
                pytest.fail("closed sandbox connection was reused")
    await manager.close()


async def test_cancelled_prepare_closes_connection_and_routes(
    manager: SandboxManager, scope: InvocationScope, guest: GuestProcess
) -> None:
    waiting = asyncio.Event()

    async def receive() -> bytes:
        waiting.set()
        return await asyncio.Future[bytes]()

    async def acquire() -> None:
        async with manager.acquire(scope, uuid7()):
            pytest.fail("sandbox became ready without a handshake")

    guest.stream.receive.side_effect = receive
    task = asyncio.create_task(acquire())
    await waiting.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    state = manager.state(scope.user_id)
    assert state.connection is None
    assert state.fiber is None
    assert not state.needs_cleanup
    assert not manager.nerve.subscribers
    assert guest.returncode is not None
    guest.stream.aclose.assert_awaited()


async def test_create_is_mountless_scoped_and_budgeted(
    manager: SandboxManager,
    scope: InvocationScope,
) -> None:
    async with manager.nerve.subscribe(UserTopic(scope.user_id)) as events:
        async with manager.acquire(scope, uuid7()):
            phases: list[str] = []
            for impulse in held(events):
                assert isinstance(impulse.event, SandboxProgress)
                phases.append(impulse.event.phase)
            assert phases == [
                "preparing",
                "starting",
                "ready",
            ]
        await manager.close()
    command = SbxSandbox.command
    assert isinstance(command, AsyncMock)
    create = next(c for c in command.await_args_list if c.args[0] == "create")
    assert create.args == ("create", "shell")
    assert create.kwargs["skills"] == "off"
    assert create.kwargs["cpus"] == 2
    assert create.kwargs["memory"] == "2048m"
    assert f"OCTOMATE_SANDBOX_USER_ID={scope.user_id}" in create.kwargs["env"]
    assert not {"mount", "volume", "workspace"}.intersection(create.kwargs)
    assert not {"rm", "prune", "reset"}.intersection(
        c.args[0] for c in command.await_args_list
    )


async def test_manager_uses_the_sandbox_protocol(
    manager: SandboxManager, scope: InvocationScope, guest: GuestProcess
) -> None:
    calls: list[str] = []

    @dataclass
    class OtherSandbox:
        name: str

        async def prepare(self) -> None:
            calls.append("prepare")

        async def open(self) -> ByteStream:
            calls.append("open")
            return guest.stream

        async def stop(self) -> None:
            calls.append("stop")

    state = manager.state(scope.user_id)
    state.sandbox = OtherSandbox(name="another-runtime")
    async with manager.acquire(scope, uuid7()):
        assert state.ready is not None
        assert manager.snapshot(scope).sandbox == "another-runtime"
    await manager.close()
    assert calls == ["prepare", "open", "stop"]
    command = SbxSandbox.command
    assert isinstance(command, AsyncMock)
    command.assert_not_called()


async def test_reuse_does_not_create_or_replace_a_sandbox(
    manager: SandboxManager,
    scope: InvocationScope,
) -> None:
    command = SbxSandbox.command
    assert isinstance(command, AsyncMock)
    command.side_effect = None
    command.return_value = manager.state(scope.user_id).sandbox.name.encode() + b"\n"
    async with manager.acquire(scope, uuid7()):
        pass
    await manager.close()
    assert "create" not in [c.args[0] for c in command.await_args_list]


@pytest.mark.parametrize("fault", ["wrong-owner", "wrong-image", "bad-json", "version"])
async def test_readiness_failure_cleans_up_and_reports_sanitized_error(
    manager: SandboxManager,
    scope: InvocationScope,
    guest: GuestProcess,
    fault: str,
) -> None:
    ready = SandboxAgentReady(
        user_id=scope.user_id,
        image=IMAGE,
        cpus=2,
        memory_mib=2048,
        accepts_invocations=False,
    )
    if fault == "wrong-owner":
        ready.user_id = uuid7()
    if fault == "wrong-image":
        ready.image = "different"
    wire = (
        Publication(topic=SandboxTopic(scope.user_id), impulse=Impulse(ready))
        .model_dump_json()
        .encode()
    )
    if fault == "bad-json":
        wire = b"secret=never-publish-this"
    if fault == "version":
        wire = wire.replace(b'"version":1', b'"version":2')
    guest.handshake = wire + b"\n"
    with pytest.raises(SandboxError):
        async with manager.acquire(scope, uuid7()):
            pytest.fail("bad sandbox agent was accepted")
    snapshot = manager.snapshot(scope)
    assert isinstance(snapshot, SandboxFailed)
    assert "never-publish" not in snapshot.message
    assert not manager.state(scope.user_id).needs_cleanup
    assert guest.returncode is not None
    await manager.close()


async def test_idle_stop_closes_attachment_only_after_last_reference(
    manager: SandboxManager,
    scope: InvocationScope,
    guest: GuestProcess,
) -> None:
    async with manager.nerve.subscribe(UserTopic(scope.user_id)) as events:
        async with manager.acquire(scope, uuid7()):
            held(events)
            assert manager.state(scope.user_id).idle is None
        async with asyncio.timeout(1):
            event = (await events.receive()).event
            assert isinstance(event, SandboxProgress)
            assert event.phase == "stopping"
            event = (await events.receive()).event
            assert isinstance(event, SandboxProgress)
            assert event.phase == "stopped"
    guest.stream.aclose.assert_awaited()
    assert not manager.state(scope.user_id).needs_cleanup
    await manager.close()


async def test_failed_stop_keeps_cleanup_pending(
    manager: SandboxManager,
    scope: InvocationScope,
) -> None:
    async with manager.acquire(scope, uuid7()):
        pass
    command = SbxSandbox.command
    assert isinstance(command, AsyncMock)
    normal = command.side_effect
    command.side_effect = SandboxError("stop failed")
    with pytest.raises(SandboxError, match="stop failed"):
        await manager.stop(scope.user_id)
    assert manager.state(scope.user_id).needs_cleanup
    assert isinstance(manager.snapshot(scope), SandboxFailed)
    command.side_effect = normal
    await manager.close()
    assert not manager.state(scope.user_id).needs_cleanup


async def test_cancelled_acquisition_does_not_cancel_an_existing_peer(
    manager: SandboxManager,
    scope: InvocationScope,
    guest: GuestProcess,
) -> None:
    async with manager.acquire(scope, uuid7()) as state:
        entered = asyncio.Event()

        async def cancelled() -> None:
            async with manager.acquire(scope, uuid7()):
                entered.set()
                await asyncio.Future[None]()

        task = asyncio.create_task(cancelled())
        await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert len(state.invocations) == 1
        assert guest.returncode is None
    await manager.close()


async def test_first_acquisitions_race_without_creating_two_vms(
    manager: SandboxManager,
    scope: InvocationScope,
    guest: GuestProcess,
) -> None:
    waiting, ready, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
    entered: asyncio.Queue[None] = asyncio.Queue()

    async def handshake() -> bytes:
        waiting.set()
        await ready.wait()
        guest.stream.receive.side_effect = guest.receive
        return await guest.receive()

    async def run() -> None:
        async with manager.acquire(scope, uuid7()):
            await entered.put(None)
            await release.wait()

    guest.stream.receive.side_effect = handshake
    first = asyncio.create_task(run())
    await waiting.wait()
    second = asyncio.create_task(run())
    ready.set()
    await entered.get()
    await entered.get()
    command = SbxSandbox.command
    assert isinstance(command, AsyncMock)
    assert sum(c.args[0] == "create" for c in command.await_args_list) == 1
    release.set()
    await asyncio.gather(first, second)
    await manager.close()


@pytest.mark.parametrize(
    ("system", "machine", "version", "error"),
    [
        ("Darwin", "arm64", "0.48.0", None),
        ("Darwin", "x86_64", "0.48.0", "Apple Silicon"),
        ("Windows", "AMD64", "0.48.0", "macOS and Linux"),
        ("Darwin", "arm64", "0.49.0", "pinned sbx release"),
    ],
)
@pytest.mark.parametrize("prefix", ["Client Version: v", "sbx version v"])
async def test_preflight_checks_host_and_exact_cli_version(
    monkeypatch: pytest.MonkeyPatch,
    system: str,
    machine: str,
    version: str,
    error: str | None,
    prefix: str,
) -> None:
    sandbox = SbxSandbox(SbxSandboxConfig(image=IMAGE), uuid7())
    monkeypatch.setattr(sandbox_module.platform, "system", lambda: system)
    monkeypatch.setattr(sandbox_module.platform, "machine", lambda: machine)
    monkeypatch.setattr(sandbox_module.platform, "mac_ver", lambda: ("14.1", (), ""))
    monkeypatch.setattr(
        sandbox,
        "command",
        AsyncMock(return_value=f"{prefix}{version} build\n".encode()),
    )
    if error is None:
        await sandbox.preflight()
    else:
        with pytest.raises(SandboxError, match=error):
            await sandbox.preflight()


@pytest.mark.parametrize(
    ("distribution", "release", "machine", "kvm", "error"),
    [
        ("ubuntu", "24.04", "x86_64", True, None),
        ("ubuntu", "24.04", "aarch64", True, None),
        ("ubuntu", "22.04", "x86_64", True, "Ubuntu 24.04"),
        ("debian", "12", "x86_64", True, "Ubuntu 24.04"),
        ("ubuntu", "24.04", "x86_64", False, "accessible KVM"),
        ("ubuntu", "24.04", "riscv64", True, "accessible KVM"),
    ],
)
async def test_linux_preflight_requires_supported_host_and_accessible_kvm(
    monkeypatch: pytest.MonkeyPatch,
    distribution: str,
    release: str,
    machine: str,
    kvm: bool,
    error: str | None,
) -> None:
    sandbox = SbxSandbox(SbxSandboxConfig(image=IMAGE), uuid7())
    monkeypatch.setattr(sandbox_module.platform, "system", lambda: "Linux")
    monkeypatch.setattr(sandbox_module.platform, "machine", lambda: machine)
    monkeypatch.setattr(
        sandbox_module.platform,
        "freedesktop_os_release",
        lambda: {"ID": distribution, "VERSION_ID": release},
    )
    monkeypatch.setattr(sandbox_module.os, "access", lambda path, mode: kvm)
    monkeypatch.setattr(
        sandbox, "command", AsyncMock(return_value=b"Client Version: v0.48.0 build\n")
    )
    if error is None:
        await sandbox.preflight()
    else:
        with pytest.raises(SandboxError, match=error):
            await sandbox.preflight()
