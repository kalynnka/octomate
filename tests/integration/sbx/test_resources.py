"""Opt-in single-user baseline; synthetic load does not represent an agent run."""

import asyncio
import json
import os
import platform
from dataclasses import asdict, dataclass
from pathlib import Path
from time import monotonic

import pytest
import sh
from anyio.streams.buffered import BufferedByteReceiveStream
from pydantic import BaseModel
from uuid_utils.compat import uuid7

from octomate.config.sandbox import SbxSandboxConfig
from octomate.managers.sandbox import SandboxManager
from octomate.nerve import Nerve
from octomate.sandbox.sbx import SbxSandbox
from octomate.schemas.sandbox import InvocationScope


class GuestMemory(BaseModel):
    total_mib: float
    available_mib: float


@dataclass
class HostUsage:
    executable: str
    rss_mib: float
    cpu_seconds: float


async def host_usage() -> dict[int, HostUsage]:
    process = sh.Command("ps")(
        "-axo", "pid=,rss=,time=,comm=", _async=True, _return_cmd=True, _tty_out=False
    )
    await process
    result: dict[int, HostUsage] = {}
    for line in process.stdout.decode().splitlines():
        pid, rss, cpu, executable = line.split(None, 3)
        if Path(executable).name not in {"sbx", "containerd-shim-nerdbox-v1"}:
            continue
        days, _, clock = cpu.rpartition("-")
        seconds = float(days or 0) * 86400
        parts = clock.split(":")
        seconds += sum(float(value) * 60**i for i, value in enumerate(reversed(parts)))
        result[int(pid)] = HostUsage(
            executable=Path(executable).name,
            rss_mib=int(rss) / 1024,
            cpu_seconds=seconds,
        )
    return result


async def sample_load(sandbox: SbxSandbox, pids: set[int]) -> float:
    peak_rss_mib = 0.0
    async with asyncio.TaskGroup() as tasks:
        load = tasks.create_task(
            sandbox.command(
                "exec",
                sandbox.name,
                "/opt/octomate/.venv/bin/python",
                "-c",
                "import hashlib,time; data=bytearray(256*1024*1024); "
                "end=time.monotonic()+5\n"
                "while time.monotonic()<end: hashlib.sha256(data).digest()",
            )
        )
        while not load.done():
            sample = await host_usage()
            peak_rss_mib = max(peak_rss_mib, sum(sample[pid].rss_mib for pid in pids))
            await asyncio.sleep(0.25)
    return peak_rss_mib


@pytest.mark.timeout(600)
async def test_single_user_resource_cost(tmp_path: Path) -> None:
    image = os.environ.get("SBX_TEST_IMAGE")
    if not image:
        pytest.fail("Set SBX_TEST_IMAGE to the immutable SDK image reference")
    config = SbxSandboxConfig(image=image)
    manager = SandboxManager(config, Nerve())
    scope = InvocationScope(user_id=uuid7(), conversation_id=uuid7())
    sandbox = manager.state(scope).sandbox
    assert isinstance(sandbox, SbxSandbox)
    assert (
        sandbox.name
        not in (await sandbox.command("ls", quiet=True)).decode().splitlines()
    )
    before = await host_usage()
    started = monotonic()
    try:
        async with manager.acquire(scope, uuid7()):
            startup_seconds = monotonic() - started
            invoked = monotonic()
            async with sandbox.execute(
                "/opt/octomate/.venv/bin/python",
                "-c",
                "import os,sys; print(os.getpid(), flush=True); sys.stdin.buffer.read()",
                workdir="/home/agent/workspace",
            ) as stream:
                process_id = int(
                    await BufferedByteReceiveStream(stream).receive_until(b"\n", 64)
                )
                await stream.send_eof()
            invocation_seconds = monotonic() - invoked
            await asyncio.sleep(2)
            idle_start = await host_usage()
            pids = idle_start.keys() - before.keys()
            assert any(
                idle_start[pid].executable == "containerd-shim-nerdbox-v1"
                for pid in pids
            ), "No new sbx VM shim found; host accounting needs updating"
            sampled = monotonic()
            await asyncio.sleep(5)
            idle_end = await host_usage()
            idle_cpu_percent = (
                100
                * sum(
                    idle_end[pid].cpu_seconds - idle_start[pid].cpu_seconds
                    for pid in pids
                )
                / (monotonic() - sampled)
            )
            guest = GuestMemory.model_validate_json(
                await sandbox.command(
                    "exec",
                    sandbox.name,
                    "/opt/octomate/.venv/bin/python",
                    "-c",
                    "import json,sys; from pathlib import Path; "
                    "assert not Path('/proc', sys.argv[1]).exists(), 'Guest process survived disconnect'; "
                    "m={k:int(v.split()[0]) for k,v in "
                    "(line.split(':',1) for line in Path('/proc/meminfo').read_text().splitlines())}; "
                    "print(json.dumps(dict(total_mib=m['MemTotal']/1024, "
                    "available_mib=m['MemAvailable']/1024)))",
                    str(process_id),
                )
            )
            peak_rss_mib = await sample_load(sandbox, pids)
        stopped = monotonic()
        await manager.stop(manager.state(scope))
        stop_seconds = monotonic() - stopped
        resumed = monotonic()
        async with manager.acquire(scope, uuid7()):
            resume_seconds = monotonic() - resumed
        report = {
            "host": f"{platform.system()} {platform.machine()}",
            "logical_cpus": os.cpu_count(),
            "image": image,
            "configured_cpus": config.cpus,
            "configured_memory_mib": config.memory_mib,
            "startup_seconds": startup_seconds,
            "process_roundtrip_seconds": invocation_seconds,
            "stop_seconds": stop_seconds,
            "resume_seconds": resume_seconds,
            "idle_processes": {pid: asdict(idle_end[pid]) for pid in pids},
            "idle_host_rss_mib": sum(idle_end[pid].rss_mib for pid in pids),
            "idle_cpu_percent_one_core": idle_cpu_percent,
            "guest": guest.model_dump(),
            "synthetic_load_mib": 256,
            "loaded_host_peak_rss_mib": peak_rss_mib,
            "scope": "new sbx attachment and VM shim RSS; excludes shared daemon, disk and real agent workloads",
        }
        output = tmp_path / "sandbox-resources.json"
        output.write_text(json.dumps(report, indent=2) + "\n")
        print(output.read_text())
        print(f"Resource report: {output}")
    finally:
        await manager.close()
        if sandbox.managed:
            await sandbox.command("rm", "--force", sandbox.name)
