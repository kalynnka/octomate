"""Shared probes and cleanup for disposable local sandbox acceptance."""

import asyncio

from uuid_utils.compat import uuid7

from octomate.events import HabitatProgressEvent
from octomate.habitat.sbx import SbxHabitat
from octomate.managers.habitat import HabitatManager
from octomate.nerve import HabitatTopic
from octomate.schemas.habitat import InvocationScope


async def remove_sandbox(sandbox: SbxHabitat) -> None:
    names = (await sandbox.control("ls", quiet=True)).decode().splitlines()
    if sandbox.id in names:
        await sandbox.control("rm", "--force", sandbox.id)


async def cli_pids(
    sandbox: SbxHabitat, *, argument_pattern: str = "--output-forma[t]"
) -> list[int]:
    paths = await sandbox.control(
        "exec",
        sandbox.id,
        "find",
        "/proc",
        "-maxdepth",
        "2",
        "-name",
        "cmdline",
        "-exec",
        "grep",
        "-lzx",
        "-e",
        # Match the CLI argument without matching this probe itself.
        argument_pattern,
        "{}",
        ";",
    )
    return [int(path.split("/")[2]) for path in paths.decode().splitlines()]


async def expire_idle(manager: HabitatManager, scope: InvocationScope) -> bytes:
    """Wait for automatic idle shutdown and return the old VM's boot identity."""
    residence = manager.residence(scope)
    sandbox = residence.habitat
    assert isinstance(sandbox, SbxHabitat)
    assert not residence.invocations
    assert residence.status.phase == "ready"
    config = sandbox.config
    sandbox.config = config.model_copy(update={"idle_timeout": 0.1})
    try:
        async with manager.nerve.subscribe(HabitatTopic(scope.user_id)) as events:
            async with manager.enter(scope, uuid7()):
                boot = await sandbox.control(
                    "exec",
                    sandbox.id,
                    "cat",
                    "/proc/sys/kernel/random/boot_id",
                )
            assert residence.idle is not None
            async with asyncio.timeout(60):
                for phase in ("stopping", "stopped"):
                    event = (await events.receive()).event
                    assert isinstance(event, HabitatProgressEvent)
                    assert event.phase == phase
                assert residence.stopping is not None
                await residence.stopping
        assert not residence.needs_cleanup
        return boot
    finally:
        sandbox.config = config
