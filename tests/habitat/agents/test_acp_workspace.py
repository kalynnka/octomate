"""ACP client workspace contracts without a provider SDK or real sandbox."""

import asyncio
import os
import signal
import sys
from collections.abc import AsyncGenerator, Callable
from contextlib import asynccontextmanager, suppress
from pathlib import Path

import acp
import anyio
import pytest
from acp import schema
from anyio.abc import ByteStream
from anyio.streams.stapled import StapledByteStream
from pydantic_ai.workspaces import WorkspaceTimeoutError
from uuid_utils.compat import uuid7

from octomate.config.habitat import SrtHabitatConfig
from octomate.habitat.executor import (
    CommandEvent,
    CommandOutput,
    CommandRequest,
    CommandResponse,
)
from octomate.habitat.srt import SrtHabitat
from octomate.tentacles.acp.workspace import AcpWorkspace, Terminal


@pytest.fixture
async def client(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> AsyncGenerator[AcpWorkspace]:
    @asynccontextmanager
    async def resident(
        self: SrtHabitat,
        *command: str,
        workdir: str,
        env: dict[str, str] | None = None,
        stderr: Callable[[str], None] | None = None,
    ) -> AsyncGenerator[ByteStream]:
        async with await anyio.open_process(
            command,
            cwd=workdir,
            env={
                "PATH": f"{Path(sys.executable).parent}{os.pathsep}{os.defpath}",
                **(env or {}),
            },
            start_new_session=True,
        ) as process:
            assert process.stdin is not None
            assert process.stdout is not None
            try:
                yield StapledByteStream(process.stdin, process.stdout)
            finally:
                with anyio.CancelScope(shield=True):
                    with suppress(ProcessLookupError):
                        os.killpg(process.pid, signal.SIGKILL)
                    await process.wait()

    monkeypatch.setattr(SrtHabitat, "resident", resident)
    habitat = SrtHabitat(config=SrtHabitatConfig(), user_id=uuid7(), root=tmp_path)
    instance = AcpWorkspace(
        habitat,
        str(tmp_path),
        session_id=str(uuid7()),
    )
    try:
        yield instance
    finally:
        await instance.close()
        await habitat.resources.aclose()


async def test_files_lines_and_missing_parents(client: AcpWorkspace) -> None:
    path = str(Path(client.cwd) / "nested/proof")
    assert (
        await client.handle(
            "fs/write_text_file",
            {
                "sessionId": client.session_id,
                "path": path,
                "content": "first\nsecond\nthird\n",
            },
        )
        == {}
    )
    assert await client.handle(
        "fs/read_text_file",
        {"sessionId": client.session_id, "path": path, "line": 2, "limit": 1},
    ) == {"content": "second\n"}
    assert await client.handle(
        "fs/read_text_file", {"sessionId": client.session_id, "path": path, "limit": 0}
    ) == {"content": ""}


async def test_rejects_cross_session_outside_and_symlink_paths(
    client: AcpWorkspace, tmp_path: Path
) -> None:
    with pytest.raises(acp.RequestError) as failure:
        await client.handle(
            "fs/read_text_file",
            {"sessionId": str(uuid7()), "path": str(tmp_path / "proof")},
        )
    assert failure.value.data == {"reason": "Unknown session"}
    root = Path(client.cwd)
    (root / "escape").symlink_to(root.parent, target_is_directory=True)
    for path in ("relative", str(root.parent / "secret"), str(root / "escape/secret")):
        with pytest.raises(acp.RequestError):
            await client.handle(
                "fs/write_text_file",
                {"sessionId": client.session_id, "path": path, "content": "no"},
            )
    with pytest.raises(acp.RequestError) as failure:
        await client.handle(
            "fs/read_text_file",
            {
                "sessionId": client.session_id,
                "path": str(tmp_path / "proof"),
                "line": 0,
            },
        )
    assert failure.value.data == {"reason": "Line numbers start at one"}


async def test_terminal_exit_environment_stderr_and_release(
    client: AcpWorkspace,
) -> None:
    created = schema.CreateTerminalResponse.model_validate(
        await client.handle(
            "terminal/create",
            {
                "sessionId": client.session_id,
                "command": sys.executable,
                "args": [
                    "-c",
                    "import os,sys; print(os.environ['PROBE']); print('diagnostic',file=sys.stderr); sys.exit(7)",
                ],
                "env": [{"name": "PROBE", "value": "explicit"}],
            },
        )
    )
    params = {"sessionId": client.session_id, "terminalId": created.terminal_id}
    assert await client.handle("terminal/wait_for_exit", params) == {"exitCode": 7}
    output = schema.TerminalOutputResponse.model_validate(
        await client.handle("terminal/output", params)
    )
    assert "explicit" in output.output
    assert "diagnostic" in output.output
    assert not output.truncated
    assert await client.handle("terminal/release", params) == {}
    with pytest.raises(acp.RequestError) as failure:
        await client.handle("terminal/output", params)
    assert failure.value.data == {"reason": "Unknown terminal"}


async def test_terminal_start_failure_preserves_diagnostic(
    client: AcpWorkspace,
) -> None:
    with pytest.raises(RuntimeError, match="octomate-nonexistent-program"):
        await client.handle(
            "terminal/create",
            {
                "sessionId": client.session_id,
                "command": "octomate-nonexistent-program",
            },
        )


@pytest.mark.parametrize("ending", ["kill", "release", "disconnect"])
async def test_running_output_cancelled_wait_kill_and_cleanup(
    client: AcpWorkspace,
    ending: str,
) -> None:
    created = schema.CreateTerminalResponse.model_validate(
        await client.handle(
            "terminal/create",
            {
                "sessionId": client.session_id,
                "command": sys.executable,
                "args": [
                    "-u",
                    "-c",
                    "import os,time; print(os.getpid()); time.sleep(30)",
                ],
            },
        )
    )
    params = {"sessionId": client.session_id, "terminalId": created.terminal_id}
    terminal = client.terminals[created.terminal_id]
    with anyio.fail_after(5):
        while not terminal.output:  # noqa: ASYNC110 - Wait for a real subprocess output boundary.
            await asyncio.sleep(0.01)
    pid = int(terminal.output)
    assert terminal.status is None
    waiting = asyncio.create_task(client.handle("terminal/wait_for_exit", params))
    await asyncio.sleep(0)
    waiting.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiting
    assert not terminal.task.done()
    if ending == "disconnect":
        await client.close()
    elif ending == "kill":
        assert await asyncio.gather(
            client.handle("terminal/kill", params),
            client.handle("terminal/kill", params),
        ) == [{}, {}]
    else:
        assert await client.handle(f"terminal/{ending}", params) == {}
    assert terminal.status is not None
    assert terminal.output == f"{pid}\n"
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)
    await client.close()
    assert not client.terminals


@pytest.mark.parametrize(("limit", "expected"), [(5, "🙂!"), (3, "!"), (0, "")])
def test_terminal_retains_utf8_tail(
    client: AcpWorkspace, limit: int, expected: str
) -> None:
    terminal = Terminal(
        client.habitat,
        client.cwd,
        schema.CreateTerminalRequest(
            session_id=str(uuid7()),
            command="unused",
            output_byte_limit=limit,
        ),
    )
    terminal.append("prefix🙂!")
    assert terminal.output == expected
    assert terminal.truncated


async def test_terminals_share_executor_and_cancel_independently(
    client: AcpWorkspace,
) -> None:
    result = await client.habitat.run(
        [sys.executable, "-c", "import os; print(os.getppid())"], workdir=client.cwd
    )
    executor_pid = int(result.stdout)
    created = [
        schema.CreateTerminalResponse.model_validate(
            await client.handle(
                "terminal/create",
                {
                    "sessionId": client.session_id,
                    "command": sys.executable,
                    "args": [
                        "-u",
                        "-c",
                        "import os,time; print(os.getpid(), os.getppid()); time.sleep(30)",
                    ],
                },
            )
        )
        for _ in range(2)
    ]
    terminals = [client.terminals[item.terminal_id] for item in created]
    with anyio.fail_after(5):
        # Wait for both real children to publish their PID and executor PID.
        while not all(terminal.output for terminal in terminals):  # noqa: ASYNC110
            await asyncio.sleep(0.01)
    pids = [list(map(int, terminal.output.split())) for terminal in terminals]
    assert [parent for _, parent in pids] == [executor_pid, executor_pid]
    await client.handle(
        "terminal/kill",
        {"sessionId": client.session_id, "terminalId": created[0].terminal_id},
    )
    with pytest.raises(ProcessLookupError):
        os.kill(pids[0][0], 0)
    os.kill(pids[1][0], 0)
    assert not terminals[1].task.done()
    await test_files_lines_and_missing_parents(client)
    result = await client.habitat.run(
        [sys.executable, "-c", "import os; print(os.getppid())"], workdir=client.cwd
    )
    assert int(result.stdout) == executor_pid


async def test_streaming_output_limit_retains_tail_without_stopping_command(
    client: AcpWorkspace,
) -> None:
    created = schema.CreateTerminalResponse.model_validate(
        await client.handle(
            "terminal/create",
            {
                "sessionId": client.session_id,
                "command": sys.executable,
                "args": [
                    "-c",
                    "import sys; sys.stdout.write('x' * (11 * 1024 * 1024) + 'tail')",
                ],
                "outputByteLimit": 4,
            },
        )
    )
    params = {"sessionId": client.session_id, "terminalId": created.terminal_id}
    assert await client.handle("terminal/wait_for_exit", params) == {"exitCode": 0}
    output = schema.TerminalOutputResponse.model_validate(
        await client.handle("terminal/output", params)
    )
    assert output.output == "tail"
    assert output.truncated


async def test_streaming_timeout_reaps_child_and_preserves_delivered_output(
    client: AcpWorkspace,
) -> None:
    events: list[CommandEvent] = []
    with pytest.raises(WorkspaceTimeoutError):
        await client.habitat.request(
            CommandRequest(
                command=[
                    sys.executable,
                    "-u",
                    "-c",
                    "import os,time; print(os.getpid()); time.sleep(30)",
                ],
                timeout=0.5,
            ),
            CommandResponse,
            workdir=client.cwd,
            on_event=events.append,
        )
    output = b"".join(
        event.data for event in events if isinstance(event, CommandOutput)
    )
    pid = int(output)
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)
    result = await client.habitat.run(["printf", "alive"], workdir=client.cwd)
    assert result.stdout == "alive"


async def test_cancelling_noisy_terminal_keeps_executor_framing_intact(
    client: AcpWorkspace,
) -> None:
    created = schema.CreateTerminalResponse.model_validate(
        await client.handle(
            "terminal/create",
            {
                "sessionId": client.session_id,
                "command": sys.executable,
                "args": [
                    "-u",
                    "-c",
                    "import sys\nwhile True: sys.stdout.write('x' * 65536)",
                ],
                "outputByteLimit": 4,
            },
        )
    )
    terminal = client.terminals[created.terminal_id]
    with anyio.fail_after(5):
        # Wait until the command is actively streaming over the shared connection.
        while not terminal.output:  # noqa: ASYNC110
            await asyncio.sleep(0.01)
    await client.handle(
        "terminal/kill",
        {"sessionId": client.session_id, "terminalId": created.terminal_id},
    )
    result = await client.habitat.run(["printf", "intact"], workdir=client.cwd)
    assert result.stdout == "intact"
    assert terminal.output == "xxxx"
    assert terminal.truncated


async def test_terminal_reports_signal_exit(client: AcpWorkspace) -> None:
    created = schema.CreateTerminalResponse.model_validate(
        await client.handle(
            "terminal/create",
            {
                "sessionId": client.session_id,
                "command": sys.executable,
                "args": [
                    "-c",
                    "import os,signal; os.kill(os.getpid(), signal.SIGTERM)",
                ],
            },
        )
    )
    assert await client.handle(
        "terminal/wait_for_exit",
        {"sessionId": client.session_id, "terminalId": created.terminal_id},
    ) == {"signal": "SIGTERM"}
