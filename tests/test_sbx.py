from __future__ import annotations

import asyncio
import os
import sys
from contextlib import suppress
from pathlib import Path

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
        "print(json.dumps(dict(argv=args, environment=dict(os.environ), "
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
    stream = await sandbox.open()
    process = sandbox.process
    assert process is not None
    receive = BufferedByteReceiveStream(stream)
    try:
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
                "--workdir=/home/agent/workspace",
                sandbox.name,
                "/opt/octomate/.venv/bin/python",
                "-m",
                "octomate.sandbox.entrypoint",
            ]
            assert process.is_alive()
            for payload in (b"first", b"\x00\xff\rsecond"):
                await stream.send(payload + b"\n")
                assert await receive.receive_until(b"\n", 65536) == payload
            await stream.send_eof()
            await process
            assert process.exit_code == 0
    finally:
        await stream.aclose()
        await sandbox.stop()
    assert sandbox.process is None


async def test_sh_failure_does_not_expose_command_output(sandbox: SbxSandbox) -> None:
    with pytest.raises(SandboxError, match=r"failed \(exit 7\)") as failure:
        await sandbox.command("fail")
    assert "synthetic-secret" not in str(failure.value)


async def test_stop_terminates_the_live_attachment(sandbox: SbxSandbox) -> None:
    stream = await sandbox.open()
    process = sandbox.process
    assert process is not None
    try:
        assert process.is_alive()
        await sandbox.stop()
        assert not process.is_alive()
        assert sandbox.process is None
    finally:
        await stream.aclose()
        await sandbox.stop()


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
    operation = sandbox.open() if attach else sandbox.command("version")
    with pytest.raises(SandboxError, match="configured executable"):
        await operation
