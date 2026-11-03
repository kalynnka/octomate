from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Unpack
from unittest.mock import AsyncMock

import pytest
from anyio.abc import ByteStream
from uuid_utils.compat import uuid7

import octomate.sandbox.sbx as sandbox_module
from octomate.config.sandbox import SbxSandboxConfig
from octomate.managers.sandbox import SandboxError, SandboxManager
from octomate.nerve import Nerve, SandboxTopic, UserTopic
from octomate.sandbox.sbx import SbxOptions, SbxSandbox
from octomate.schemas.sandbox import InvocationScope, SandboxFailed, SandboxProgress
from tests.support.nerve import held
from tests.test_sandbox_contracts import IMAGE


@pytest.fixture
def scope() -> InvocationScope:
    return InvocationScope(user_id=uuid7(), mode="sbx", conversation_id=uuid7())


@pytest.fixture
def manager(monkeypatch: pytest.MonkeyPatch, scope: InvocationScope) -> SandboxManager:
    manager = SandboxManager(SbxSandboxConfig(image=IMAGE, idle_timeout=0.01), Nerve())
    monkeypatch.setattr(SbxSandbox, "preflight", AsyncMock())
    monkeypatch.setattr(
        SbxSandbox,
        "execute",
        AsyncMock(side_effect=AssertionError("acquisition must not launch an agent")),
    )
    names: set[str] = set()

    async def command(*args: str, **options: Unpack[SbxOptions]) -> bytes:
        if args[0] == "create":
            assert "name" in options
            names.add(options["name"])
        if args[0] == "exec":
            assert args[2] == "printenv"
            return f"{scope.user_id}\n{IMAGE}\n2\n2048\n".encode()
        return "\n".join(sorted(names)).encode() if args[0] == "ls" else b""

    monkeypatch.setattr(SbxSandbox, "command", AsyncMock(side_effect=command))
    return manager


async def test_concurrent_acquisitions_share_one_vm_and_keep_peer_alive(
    manager: SandboxManager, scope: InvocationScope
) -> None:
    entered, release = asyncio.Event(), asyncio.Event()

    async def peer() -> None:
        async with manager.acquire(scope, uuid7()):
            entered.set()
            await release.wait()

    task = asyncio.create_task(peer())
    await entered.wait()
    async with manager.nerve.subscribe(UserTopic(scope.user_id)) as events:
        async with manager.acquire(scope, uuid7()) as state:
            assert len(state.invocations) == 2
            assert state.snapshot.phase == "ready"
        await manager.stop(scope.user_id)
        assert state.needs_cleanup
        assert held(events) == []
    release.set()
    await task
    await manager.close()
    assert manager.snapshot(scope).phase == "stopped"
    assert not state.needs_cleanup
    assert not manager.nerve.subscribers
    execute = SbxSandbox.execute
    assert isinstance(execute, AsyncMock)
    execute.assert_not_called()


async def test_cancelled_prepare_stops_runtime_without_launching_an_agent(
    manager: SandboxManager, scope: InvocationScope, monkeypatch: pytest.MonkeyPatch
) -> None:
    waiting = asyncio.Event()
    runtime_prepare = manager.state(scope.user_id).sandbox.prepare

    async def prepare() -> None:
        await runtime_prepare()
        waiting.set()
        await asyncio.Future[None]()

    async def acquire() -> None:
        async with manager.acquire(scope, uuid7()):
            pytest.fail("sandbox became ready before startup completed")

    monkeypatch.setattr(manager.state(scope.user_id).sandbox, "prepare", prepare)
    task = asyncio.create_task(acquire())
    await waiting.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not manager.state(scope.user_id).needs_cleanup
    assert manager.snapshot(scope).phase == "failed"
    assert not manager.nerve.subscribers
    command = SbxSandbox.command
    assert isinstance(command, AsyncMock)
    command.assert_any_await("stop", manager.state(scope.user_id).sandbox.name)


async def test_create_is_mountless_scoped_and_budgeted(
    manager: SandboxManager, scope: InvocationScope
) -> None:
    async with (
        manager.nerve.subscribe(UserTopic(scope.user_id)) as events,
        manager.nerve.subscribe(SandboxTopic(scope.user_id)) as lifecycle,
    ):
        async with manager.acquire(scope, uuid7()):
            observations = held(events)
            assert held(lifecycle) == observations
            phases: list[str] = []
            for impulse in observations:
                assert isinstance(impulse.event, SandboxProgress)
                phases.append(impulse.event.phase)
            assert phases == ["preparing", "ready"]
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
    manager: SandboxManager, scope: InvocationScope
) -> None:
    calls: list[str] = []

    @dataclass
    class OtherSandbox:
        name: str

        async def prepare(self) -> None:
            calls.append("prepare")

        @asynccontextmanager
        async def execute(
            self, *command: str, workdir: str, env: dict[str, str] | None = None
        ) -> AsyncGenerator[ByteStream]:
            pytest.fail("acquisition must not launch an agent")
            yield AsyncMock(spec=ByteStream)

        async def stop(self) -> None:
            calls.append("stop")

    state = manager.state(scope.user_id)
    state.sandbox = OtherSandbox(name="another-runtime")
    async with manager.acquire(scope, uuid7()):
        assert state.snapshot.phase == "ready"
        assert manager.snapshot(scope).sandbox == "another-runtime"
    await manager.close()
    assert calls == ["prepare", "stop"]
    command = SbxSandbox.command
    assert isinstance(command, AsyncMock)
    command.assert_not_called()


async def test_reuse_does_not_create_or_replace_a_sandbox(
    manager: SandboxManager, scope: InvocationScope
) -> None:
    command = SbxSandbox.command
    assert isinstance(command, AsyncMock)
    await manager.state(scope.user_id).sandbox.prepare()
    command.reset_mock()
    async with manager.acquire(scope, uuid7()):
        pass
    await manager.close()
    assert "create" not in [c.args[0] for c in command.await_args_list]


@pytest.mark.parametrize("field", [0, 1, 2, 3])
async def test_configuration_mismatch_stops_runtime_without_replacing_it(
    manager: SandboxManager, scope: InvocationScope, field: int
) -> None:
    command = SbxSandbox.command
    assert isinstance(command, AsyncMock)
    metadata = [str(scope.user_id).encode(), IMAGE.encode(), b"2", b"2048"]
    metadata[field] = b"mismatch"

    async def existing(*args: str, **options: Unpack[SbxOptions]) -> bytes:
        if args[0] == "ls":
            return manager.state(scope.user_id).sandbox.name.encode() + b"\n"
        return b"\n".join(metadata)

    command.side_effect = existing
    with pytest.raises(SandboxError, match="settings differ"):
        async with manager.acquire(scope, uuid7()):
            pytest.fail("mismatched sandbox was accepted")
    assert isinstance(manager.snapshot(scope), SandboxFailed)
    assert not manager.state(scope.user_id).needs_cleanup
    assert "create" not in [c.args[0] for c in command.await_args_list]
    command.assert_any_await("stop", manager.state(scope.user_id).sandbox.name)
    await manager.close()


async def test_idle_stop_runs_only_after_last_reference(
    manager: SandboxManager, scope: InvocationScope
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
    assert not manager.state(scope.user_id).needs_cleanup
    await manager.close()


async def test_failed_stop_keeps_cleanup_pending(
    manager: SandboxManager, scope: InvocationScope
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
    manager: SandboxManager, scope: InvocationScope
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
        assert state.snapshot.phase == "ready"
    await manager.close()


async def test_first_acquisitions_race_without_creating_two_vms(
    manager: SandboxManager, scope: InvocationScope, monkeypatch: pytest.MonkeyPatch
) -> None:
    waiting, ready, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
    entered: asyncio.Queue[None] = asyncio.Queue()
    prepare = AsyncMock(wraps=manager.state(scope.user_id).sandbox.prepare)

    async def preparing() -> None:
        waiting.set()
        await ready.wait()
        await prepare()

    async def run() -> None:
        async with manager.acquire(scope, uuid7()):
            await entered.put(None)
            await release.wait()

    monkeypatch.setattr(manager.state(scope.user_id).sandbox, "prepare", preparing)
    first = asyncio.create_task(run())
    await waiting.wait()
    second = asyncio.create_task(run())
    ready.set()
    await entered.get()
    await entered.get()
    command = SbxSandbox.command
    assert isinstance(command, AsyncMock)
    assert sum(c.args[0] == "create" for c in command.await_args_list) == 1
    prepare.assert_awaited_once()
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
