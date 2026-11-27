"""Completed commands have the same contract on native and managed habitats."""

import asyncio
import os
import signal
from collections.abc import AsyncGenerator
from pathlib import Path
from typing import Literal

import anyio
import pytest
from pydantic_ai.workspaces import WorkspaceTimeoutError, WorkspaceUnavailableError
from uuid_utils.compat import uuid7

from octomate.config.habitat import SbxHabitatConfig
from octomate.habitat import Habitat, ManagedHabitat, NativeHabitat
from octomate.habitat.sbx import SbxHabitat
from tests.habitat.agents.test_claude import executable as executable
from tests.habitat.test_contracts import IMAGE


@pytest.fixture(params=["native", "sbx"])
async def habitat(
    request: pytest.FixtureRequest, executable: Path, tmp_path: Path
) -> AsyncGenerator[Habitat]:
    if request.param == "native":
        yield NativeHabitat()
        return
    sandbox = SbxHabitat(
        config=SbxHabitatConfig(image=IMAGE, executable=str(executable)),
        user_id=uuid7(),
        root=tmp_path,
        config_dir=tmp_path,
    )

    try:
        yield sandbox
    finally:
        await sandbox.resources.aclose()


async def test_run_captures_output_exit_and_explicit_environment(
    habitat: Habitat, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HABITAT_PRIVATE_VALUE", "core-only")
    result = await habitat.run(
        [
            "python",
            "-c",
            "import os,sys; "
            "assert sys.stdin.read() == ''; "
            "assert 'HABITAT_PRIVATE_VALUE' not in os.environ; "
            "print(os.getcwd()); print(os.environ['RUN_VALUE']); "
            "sys.stderr.buffer.write(b'diagnostic\\xff'); sys.exit(7)",
        ],
        workdir=str(tmp_path),
        env={"RUN_VALUE": "explicit"},
    )
    assert result.exit_code == 7
    assert result.stdout.splitlines() == [str(tmp_path), "explicit"]
    assert result.stderr == "diagnostic\ufffd"


async def test_concurrent_runs_keep_their_directories_separate(
    habitat: Habitat, tmp_path: Path
) -> None:
    first, second = tmp_path / "first", tmp_path / "second"
    first.mkdir()
    second.mkdir()
    results = await asyncio.gather(
        habitat.run("printf one > proof; pwd", shell=True, workdir=str(first)),
        habitat.run("printf two > proof; pwd", shell=True, workdir=str(second)),
    )
    assert [result.stdout.strip() for result in results] == [str(first), str(second)]
    assert (first / "proof").read_text() == "one"
    assert (second / "proof").read_text() == "two"


async def test_run_timeout_retains_output(habitat: Habitat, tmp_path: Path) -> None:
    with pytest.raises(WorkspaceTimeoutError) as failure:
        await habitat.run(
            "printf before; printf problem >&2; sleep 30",
            shell=True,
            workdir=str(tmp_path),
            timeout=0.2,
        )
    assert failure.value.stdout == "before"
    assert failure.value.stderr == "problem"


async def test_missing_program_returns_127(habitat: Habitat, tmp_path: Path) -> None:
    result = await habitat.run(["octomate-nonexistent-program"], workdir=str(tmp_path))
    assert result.exit_code == 127
    assert result.stderr


async def test_cancellation_reaps_the_command(habitat: Habitat, tmp_path: Path) -> None:
    running = asyncio.create_task(
        habitat.run(
            "echo $$ > command.pid; exec sleep 30", shell=True, workdir=str(tmp_path)
        )
    )
    try:
        with anyio.fail_after(10):
            # The subprocess signals readiness through a file, not an asyncio event.
            while not (tmp_path / "command.pid").exists():  # noqa: ASYNC110
                await asyncio.sleep(0.02)
        pid = int((tmp_path / "command.pid").read_text())
    finally:
        running.cancel()
        with pytest.raises(asyncio.CancelledError):
            await running
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)
    result = await habitat.run(["printf", "still-running"], workdir=str(tmp_path))
    assert result.stdout == "still-running"


@pytest.mark.parametrize("case", ["string", "argv", "empty", "timeout"])
async def test_invalid_command_is_rejected(
    habitat: Habitat,
    tmp_path: Path,
    case: Literal["string", "argv", "empty", "timeout"],
) -> None:
    with pytest.raises(TypeError if case in {"string", "argv"} else ValueError):
        await habitat.run(
            "pwd" if case == "string" else [] if case == "empty" else ["pwd"],
            shell=case == "argv",
            workdir=str(tmp_path),
            timeout=0 if case == "timeout" else None,
        )


async def test_retained_executor_reuses_imports_and_runs_operations_concurrently(
    habitat: Habitat,
    tmp_path: Path,
) -> None:
    if not isinstance(habitat, ManagedHabitat):
        pytest.skip("Native execution does not use the managed executor")
    command = ["python", "-c", "import os; print(os.getppid())"]
    first = await habitat.run(command, workdir=str(tmp_path))
    second = await habitat.run(command, workdir=str(tmp_path))
    assert first.stdout == second.stdout
    script = (
        "import sys,time; from pathlib import Path; "
        "Path(sys.argv[1]).touch()\n"
        "while not Path(sys.argv[2]).exists(): time.sleep(0.01)\n"
        "print(sys.argv[1])"
    )
    results = await asyncio.gather(
        habitat.run(
            ["python", "-c", script, "left", "right"], workdir=str(tmp_path), timeout=5
        ),
        habitat.run(
            ["python", "-c", script, "right", "left"], workdir=str(tmp_path), timeout=5
        ),
    )
    assert [result.stdout.strip() for result in results] == ["left", "right"]
    executor_pid = int(first.stdout)
    await habitat.resources.aclose()
    with pytest.raises(ProcessLookupError):
        os.kill(executor_pid, 0)
    restarted = await habitat.run(command, workdir=str(tmp_path))
    assert restarted.stdout != first.stdout


async def test_failed_executor_is_reported_without_automatic_restart(
    habitat: Habitat,
    tmp_path: Path,
) -> None:
    if not isinstance(habitat, ManagedHabitat):
        pytest.skip("Native execution does not use the managed executor")
    result = await habitat.run(
        ["python", "-c", "import os; print(os.getppid())"], workdir=str(tmp_path)
    )
    client = habitat.executor
    assert client is not None
    os.kill(int(result.stdout), signal.SIGTERM)
    await client.task
    with pytest.raises(WorkspaceUnavailableError, match="executor is unavailable"):
        await habitat.run(["printf", "unexpected"], workdir=str(tmp_path))
    assert habitat.executor is client


async def test_executor_shutdown_reaps_active_commands(
    habitat: Habitat,
    tmp_path: Path,
) -> None:
    if not isinstance(habitat, ManagedHabitat):
        pytest.skip("Native execution does not use the managed executor")
    running = asyncio.create_task(
        habitat.run(
            "echo $$ > active.pid; exec sleep 30",
            shell=True,
            workdir=str(tmp_path),
        )
    )
    with anyio.fail_after(10):
        while not (tmp_path / "active.pid").exists():  # noqa: ASYNC110
            await asyncio.sleep(0.02)
    pid = int((tmp_path / "active.pid").read_text())
    await habitat.stop()
    with pytest.raises(WorkspaceUnavailableError, match="executor stopped"):
        await running
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)
