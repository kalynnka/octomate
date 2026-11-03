from __future__ import annotations

import asyncio
import os
import sys
from contextlib import suppress
from pathlib import Path

import anyio
import pytest
from anyio.streams.buffered import BufferedByteReceiveStream
from pydantic import BaseModel
from uuid_utils.compat import uuid7

import octomate.sandbox.sbx as sbx_module
from octomate.config.sandbox import SbxSandboxConfig
from octomate.sandbox import SandboxError
from octomate.sandbox.sbx import SbxSandbox
from tests.test_sandbox_contracts import IMAGE


class ProcessInfo(BaseModel):
    pid: int
    argv: list[str]
    environment: dict[str, str]
    tty: list[bool]


@pytest.fixture
def sandbox(tmp_path: Path) -> SbxSandbox:
    executable = tmp_path / "fake sbx"
    executable.write_text(
        f"#!{sys.executable}\n"
        "import json, os, signal, sys\n"
        "from pathlib import Path\n"
        "args = sys.argv[1:]\n"
        "if args[1] == 'fail':\n"
        "    print('synthetic-secret', file=sys.stderr)\n"
        "    print('synthetic-secret')\n"
        "    sys.exit(7)\n"
        "if args[1] == 'wait':\n"
        "    Path(args[2]).write_text(str(os.getpid()))\n"
        "    print('started', flush=True)\n"
        "    signal.pause()\n"
        "print(json.dumps(dict(pid=os.getpid(), argv=args, environment=dict(os.environ), "
        "tty=[os.isatty(0), os.isatty(1)])), flush=True)\n"
        "if args[1] == 'exec':\n"
        "    for line in sys.stdin.buffer:\n"
        "        sys.stdout.buffer.write(line)\n"
        "        sys.stdout.buffer.flush()\n"
    )
    executable.chmod(0o755)
    return SbxSandbox(
        config=SbxSandboxConfig(image=IMAGE, executable=str(executable)),
        user_id=uuid7(),
    )


async def test_sh_preserves_named_arguments_and_filters_host_environment(
    sandbox: SbxSandbox, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "synthetic-secret")
    monkeypatch.setenv("OCTOMATE_DB_URL", "synthetic-secret")
    name = "literal spaces; $(false)"
    result = ProcessInfo.model_validate_json(
        await sandbox.command(
            "create",
            "shell",
            name=name,
            template=IMAGE,
            cpus=2,
            memory="2048m",
            no_progress=True,
            env=["FIRST=spaces and = signs", "SECOND=$literal"],
        )
    )
    assert result.argv == [
        "--debug=false",
        "create",
        "shell",
        f"--name={name}",
        f"--template={IMAGE}",
        "--cpus=2",
        "--memory=2048m",
        "--no-progress",
        "--env=FIRST=spaces and = signs",
        "--env=SECOND=$literal",
    ]
    assert "ANTHROPIC_API_KEY" not in result.environment
    assert "OCTOMATE_DB_URL" not in result.environment
    assert result.tty == [False, False]


async def test_sh_attachment_preserves_duplex_bytes_and_eof(
    sandbox: SbxSandbox, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "synthetic-secret")
    async with sandbox.execute(
        "python",
        "-c",
        "literal spaces; $(false)",
        workdir="/workspace",
        env={"INVOCATION": "literal value"},
    ) as stream:
        receive = BufferedByteReceiveStream(stream)
        async with asyncio.timeout(5):
            info = ProcessInfo.model_validate_json(
                await receive.receive_until(b"\n", 65536)
            )
            assert info.tty == [False, False]
            assert "ANTHROPIC_API_KEY" not in info.environment
            assert "OCTOMATE_DB_URL" not in info.environment
            assert info.argv == [
                "--debug=false",
                "exec",
                "--interactive",
                "--workdir=/workspace",
                "--env=INVOCATION=literal value",
                sandbox.name,
                "python",
                "-c",
                "literal spaces; $(false)",
            ]
            for payload in (b"first", b"\x00\xff\rsecond"):
                await stream.send(payload + b"\n")
                assert await receive.receive_until(b"\n", 65536) == payload
            await stream.send_eof()
            with pytest.raises(anyio.EndOfStream):
                await receive.receive()
    with pytest.raises(ProcessLookupError):
        os.kill(info.pid, 0)


async def test_sh_failure_does_not_expose_command_output(sandbox: SbxSandbox) -> None:
    with pytest.raises(SandboxError, match=r"failed \(exit 7\)") as failure:
        await sandbox.command("fail")
    assert "synthetic-secret" not in str(failure.value)


@pytest.mark.parametrize("cancel", [False, True])
async def test_command_context_reaps_its_child_without_stopping_the_vm(
    sandbox: SbxSandbox, cancel: bool
) -> None:
    started: asyncio.Future[ProcessInfo] = asyncio.Future()

    async def run() -> None:
        async with sandbox.execute("cat", workdir="/workspace") as stream:
            receive = BufferedByteReceiveStream(stream)
            started.set_result(
                ProcessInfo.model_validate_json(
                    await receive.receive_until(b"\n", 65536)
                )
            )
            if cancel:
                await asyncio.Future[None]()

    task = asyncio.create_task(run())
    info = await asyncio.wait_for(started, 5)
    if cancel:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    else:
        await task
    with pytest.raises(ProcessLookupError):
        os.kill(info.pid, 0)


async def test_concurrent_commands_have_independent_lifetimes(
    sandbox: SbxSandbox,
) -> None:
    async with sandbox.execute("cat", workdir="/workspace") as first:
        receive = BufferedByteReceiveStream(first)
        first_info = ProcessInfo.model_validate_json(
            await receive.receive_until(b"\n", 65536)
        )
        async with sandbox.execute("cat", workdir="/workspace") as second:
            second_info = ProcessInfo.model_validate_json(
                await BufferedByteReceiveStream(second).receive_until(b"\n", 65536)
            )
            assert first_info.pid != second_info.pid
        with pytest.raises(ProcessLookupError):
            os.kill(second_info.pid, 0)
        await first.send(b"still running\n")
        assert await receive.receive_until(b"\n", 65536) == b"still running"
    with pytest.raises(ProcessLookupError):
        os.kill(first_info.pid, 0)


@pytest.mark.parametrize("cancel", [False, True])
async def test_sh_timeout_and_cancellation_reap_child(
    sandbox: SbxSandbox, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, cancel: bool
) -> None:
    monkeypatch.setattr(sbx_module, "COMMAND_TIMEOUT", 5 if cancel else 0.3)
    started = asyncio.Event()
    loop = asyncio.get_running_loop()
    command = sandbox.cli().bake(
        _out=lambda line: loop.call_soon_threadsafe(started.set)
    )
    monkeypatch.setattr(sandbox, "cli", lambda: command)
    pid_file = tmp_path / "child.pid"
    task = asyncio.create_task(sandbox.command("wait", str(pid_file)))
    try:
        async with asyncio.timeout(5):
            await started.wait()
            if cancel:
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
            else:
                with pytest.raises(SandboxError, match="timed out"):
                    await task
        with pytest.raises(ProcessLookupError):
            os.kill(int(pid_file.read_text()), 0)
    finally:
        task.cancel()
        with suppress(asyncio.CancelledError, SandboxError):
            await task


@pytest.mark.parametrize("attach", [False, True])
async def test_missing_sbx_has_an_actionable_error(
    tmp_path: Path, attach: bool
) -> None:
    sandbox = SbxSandbox(
        config=SbxSandboxConfig(image=IMAGE, executable=str(tmp_path / "missing")),
        user_id=uuid7(),
    )
    if attach:
        with pytest.raises(SandboxError, match="configured executable"):
            async with sandbox.execute("cat", workdir="/workspace"):
                pytest.fail("missing executable launched")
    else:
        with pytest.raises(SandboxError, match="configured executable"):
            await sandbox.command("version")
