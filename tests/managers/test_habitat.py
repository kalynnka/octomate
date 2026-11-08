from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Unpack
from unittest.mock import AsyncMock

import pytest
from anyio.abc import ByteStream
from uuid_utils.compat import uuid7

import octomate.habitat.sbx as sbx_module
from octomate.config.habitat import SbxHabitatConfig
from octomate.habitat import Habitat
from octomate.habitat.sbx import SbxHabitat, SbxOptions
from octomate.managers.habitat import HabitatError, HabitatManager
from octomate.nerve import HabitatTopic, Nerve, UserTopic
from octomate.schemas.habitat import HabitatFailed, HabitatProgress, InvocationScope
from tests.support.nerve import held
from tests.test_habitat_contracts import IMAGE


@pytest.fixture
def scope() -> InvocationScope:
    return InvocationScope(user_id=uuid7(), conversation_id=uuid7())


@pytest.fixture
def manager(monkeypatch: pytest.MonkeyPatch, scope: InvocationScope) -> HabitatManager:
    manager = HabitatManager(SbxHabitatConfig(image=IMAGE, idle_timeout=0.01), Nerve())
    monkeypatch.setattr(SbxHabitat, "preflight", AsyncMock())
    monkeypatch.setattr(
        SbxHabitat,
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
            assert isinstance(manager.config, SbxHabitatConfig)
            internet_access = str(manager.config.internet_access).lower()
            return f"{scope.user_id}\n{IMAGE}\n2\n2048\n{internet_access}\n".encode()
        return "\n".join(sorted(names)).encode() if args[0] == "ls" else b""

    monkeypatch.setattr(SbxHabitat, "command", AsyncMock(side_effect=command))
    return manager


async def test_concurrent_acquisitions_share_one_vm_and_keep_peer_alive(
    manager: HabitatManager, scope: InvocationScope
) -> None:
    entered, release = asyncio.Event(), asyncio.Event()

    async def peer() -> None:
        async with manager.enter(scope, uuid7()):
            entered.set()
            await release.wait()

    task = asyncio.create_task(peer())
    await entered.wait()
    async with manager.nerve.subscribe(UserTopic(scope.user_id)) as events:
        async with manager.enter(scope, uuid7()) as state:
            assert len(state.invocations) == 2
            assert state.status.phase == "ready"
        await manager.stop(manager.residence(scope))
        assert state.needs_cleanup
        assert held(events) == []
    release.set()
    await task
    await manager.close()
    assert manager.status(scope).phase == "stopped"
    assert not state.needs_cleanup
    assert not manager.nerve.subscribers
    execute = SbxHabitat.execute
    assert isinstance(execute, AsyncMock)
    execute.assert_not_called()


async def test_cancelled_prepare_stops_runtime_without_launching_an_agent(
    manager: HabitatManager, scope: InvocationScope, monkeypatch: pytest.MonkeyPatch
) -> None:
    waiting = asyncio.Event()
    runtime_prepare = manager.residence(scope).habitat.prepare

    async def prepare() -> None:
        await runtime_prepare()
        waiting.set()
        await asyncio.Future[None]()

    async def acquire() -> None:
        async with manager.enter(scope, uuid7()):
            pytest.fail("habitat became ready before startup completed")

    monkeypatch.setattr(manager.residence(scope).habitat, "prepare", prepare)
    task = asyncio.create_task(acquire())
    await waiting.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not manager.residence(scope).needs_cleanup
    assert manager.status(scope).phase == "failed"
    assert not manager.nerve.subscribers
    command = SbxHabitat.command
    assert isinstance(command, AsyncMock)
    command.assert_any_await("stop", manager.residence(scope).habitat.name)


@pytest.mark.parametrize("internet_access", [True, False])
async def test_create_is_mountless_scoped_and_budgeted(
    manager: HabitatManager, scope: InvocationScope, internet_access: bool
) -> None:
    manager.config = SbxHabitatConfig(
        image=IMAGE, idle_timeout=0.01, internet_access=internet_access
    )
    async with (
        manager.nerve.subscribe(UserTopic(scope.user_id)) as events,
        manager.nerve.subscribe(HabitatTopic(scope.user_id)) as lifecycle,
    ):
        async with manager.enter(scope, uuid7()):
            observations = held(events)
            assert held(lifecycle) == observations
            phases: list[str] = []
            for impulse in observations:
                assert isinstance(impulse.event, HabitatProgress)
                phases.append(impulse.event.phase)
            assert phases == ["preparing", "ready"]
        await manager.close()
    command = SbxHabitat.command
    assert isinstance(command, AsyncMock)
    create = next(c for c in command.await_args_list if c.args[0] == "create")
    assert create.args[0] == "create"
    assert (Path(create.args[1]) / "spec.yaml").is_file()
    assert create.kwargs["kit_arg"] == [
        f"user_id={scope.user_id}",
        f"image={IMAGE}",
        "cpus=2",
        "memory_mib=2048",
        f"internet_access={str(internet_access).lower()}",
    ]
    kits = create.kwargs["kit"]
    assert kits == ([] if internet_access else [str(Path(create.args[1]) / "offline")])
    for kit in kits:
        assert (Path(kit) / "spec.yaml").is_file()
    assert create.kwargs["skills"] == "off"
    assert not {"template", "cpus", "memory", "env", "deny_network"}.intersection(
        create.kwargs
    )
    assert not {"mount", "volume", "workspace"}.intersection(create.kwargs)
    assert not {"rm", "prune", "reset"}.intersection(
        c.args[0] for c in command.await_args_list
    )


async def test_manager_uses_the_habitat_base_class(
    manager: HabitatManager, scope: InvocationScope
) -> None:
    calls: list[str] = []

    class OtherHabitat(Habitat):
        @property
        def name(self) -> str:
            return "another-runtime"

        @property
        def idle_timeout(self) -> float:
            return 0.01

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

    state = manager.residence(scope)
    state.habitat = OtherHabitat()
    async with manager.enter(scope, uuid7()):
        assert state.status.phase == "ready"
        assert manager.status(scope).habitat == "another-runtime"
    await manager.close()
    assert calls == ["prepare", "stop"]
    command = SbxHabitat.command
    assert isinstance(command, AsyncMock)
    command.assert_not_called()


async def test_reuse_does_not_create_or_replace_a_habitat(
    manager: HabitatManager, scope: InvocationScope
) -> None:
    command = SbxHabitat.command
    assert isinstance(command, AsyncMock)
    await manager.residence(scope).habitat.prepare()
    command.reset_mock()
    async with manager.enter(scope, uuid7()):
        pass
    await manager.close()
    assert "create" not in [c.args[0] for c in command.await_args_list]


@pytest.mark.parametrize("field", [0, 1, 2, 3, 4])
async def test_configuration_mismatch_stops_runtime_without_replacing_it(
    manager: HabitatManager, scope: InvocationScope, field: int
) -> None:
    command = SbxHabitat.command
    assert isinstance(command, AsyncMock)
    metadata = [
        str(scope.user_id).encode(),
        IMAGE.encode(),
        b"2",
        b"2048",
        b"true",
    ]
    metadata[field] = b"mismatch"

    async def existing(*args: str, **options: Unpack[SbxOptions]) -> bytes:
        if args[0] == "ls":
            return manager.residence(scope).habitat.name.encode() + b"\n"
        return b"\n".join(metadata)

    command.side_effect = existing
    with pytest.raises(HabitatError, match="settings differ"):
        async with manager.enter(scope, uuid7()):
            pytest.fail("mismatched habitat was accepted")
    assert isinstance(manager.status(scope), HabitatFailed)
    assert not manager.residence(scope).needs_cleanup
    assert "create" not in [c.args[0] for c in command.await_args_list]
    command.assert_any_await("stop", manager.residence(scope).habitat.name)
    await manager.close()


async def test_idle_stop_runs_only_after_last_reference(
    manager: HabitatManager, scope: InvocationScope
) -> None:
    async with manager.nerve.subscribe(UserTopic(scope.user_id)) as events:
        async with manager.enter(scope, uuid7()):
            held(events)
            assert manager.residence(scope).idle is None
        async with asyncio.timeout(1):
            event = (await events.receive()).event
            assert isinstance(event, HabitatProgress)
            assert event.phase == "stopping"
            event = (await events.receive()).event
            assert isinstance(event, HabitatProgress)
            assert event.phase == "stopped"
    assert not manager.residence(scope).needs_cleanup
    await manager.close()


async def test_failed_stop_keeps_cleanup_pending(
    manager: HabitatManager, scope: InvocationScope
) -> None:
    async with manager.enter(scope, uuid7()):
        pass
    command = SbxHabitat.command
    assert isinstance(command, AsyncMock)
    normal = command.side_effect
    command.side_effect = HabitatError("stop failed")
    with pytest.raises(HabitatError, match="stop failed"):
        await manager.stop(manager.residence(scope))
    assert manager.residence(scope).needs_cleanup
    assert isinstance(manager.status(scope), HabitatFailed)
    command.side_effect = normal
    await manager.close()
    assert not manager.residence(scope).needs_cleanup


async def test_cancelled_acquisition_does_not_cancel_an_existing_peer(
    manager: HabitatManager, scope: InvocationScope
) -> None:
    async with manager.enter(scope, uuid7()) as state:
        entered = asyncio.Event()

        async def cancelled() -> None:
            async with manager.enter(scope, uuid7()):
                entered.set()
                await asyncio.Future[None]()

        task = asyncio.create_task(cancelled())
        await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert len(state.invocations) == 1
        assert state.status.phase == "ready"
    await manager.close()


async def test_first_acquisitions_race_without_creating_two_vms(
    manager: HabitatManager, scope: InvocationScope, monkeypatch: pytest.MonkeyPatch
) -> None:
    waiting, ready, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
    entered: asyncio.Queue[None] = asyncio.Queue()
    prepare = AsyncMock(wraps=manager.residence(scope).habitat.prepare)

    async def preparing() -> None:
        waiting.set()
        await ready.wait()
        await prepare()

    async def run() -> None:
        async with manager.enter(scope, uuid7()):
            await entered.put(None)
            await release.wait()

    monkeypatch.setattr(manager.residence(scope).habitat, "prepare", preparing)
    first = asyncio.create_task(run())
    await waiting.wait()
    second = asyncio.create_task(run())
    ready.set()
    await entered.get()
    await entered.get()
    command = SbxHabitat.command
    assert isinstance(command, AsyncMock)
    assert sum(c.args[0] == "create" for c in command.await_args_list) == 1
    prepare.assert_awaited_once()
    release.set()
    await asyncio.gather(first, second)
    await manager.close()


def sbx_replies(version: str, forwarding: str = "false") -> AsyncMock:
    replies = {"version": version, "settings": f"{forwarding}\n"}
    return AsyncMock(side_effect=lambda *args: replies[args[0]].encode())


@pytest.mark.parametrize(
    ("system", "machine", "version", "forwarding", "error"),
    [
        ("Darwin", "arm64", "0.48.0", "false", None),
        ("Darwin", "x86_64", "0.48.0", "false", "Apple Silicon"),
        ("Windows", "AMD64", "0.48.0", "false", "macOS and Linux"),
        ("Darwin", "arm64", "0.49.0", "false", "pinned sbx release"),
        ("Darwin", "arm64", "0.48.0", "true", "SSH agent forwarding"),
    ],
)
@pytest.mark.parametrize("prefix", ["Client Version: v", "sbx version v"])
async def test_preflight_checks_host_cli_version_and_ssh_forwarding(
    monkeypatch: pytest.MonkeyPatch,
    system: str,
    machine: str,
    version: str,
    forwarding: str,
    error: str | None,
    prefix: str,
) -> None:
    habitat = SbxHabitat(SbxHabitatConfig(image=IMAGE), uuid7())
    monkeypatch.setattr(sbx_module.platform, "system", lambda: system)
    monkeypatch.setattr(sbx_module.platform, "machine", lambda: machine)
    monkeypatch.setattr(sbx_module.platform, "mac_ver", lambda: ("14.1", (), ""))
    monkeypatch.setattr(
        habitat, "command", sbx_replies(f"{prefix}{version} build\n", forwarding)
    )
    if error is None:
        await habitat.preflight()
    else:
        with pytest.raises(HabitatError, match=error):
            await habitat.preflight()


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
    habitat = SbxHabitat(SbxHabitatConfig(image=IMAGE), uuid7())
    monkeypatch.setattr(sbx_module.platform, "system", lambda: "Linux")
    monkeypatch.setattr(sbx_module.platform, "machine", lambda: machine)
    monkeypatch.setattr(
        sbx_module.platform,
        "freedesktop_os_release",
        lambda: {"ID": distribution, "VERSION_ID": release},
    )
    monkeypatch.setattr(sbx_module.os, "access", lambda path, mode: kvm)
    monkeypatch.setattr(
        habitat, "command", sbx_replies("Client Version: v0.48.0 build\n")
    )
    if error is None:
        await habitat.preflight()
    else:
        with pytest.raises(HabitatError, match=error):
            await habitat.preflight()
