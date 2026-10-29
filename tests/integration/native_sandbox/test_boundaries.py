"""Strict expectations stay failing when a native runtime permits an escape."""

import asyncio
import os
import re

import anyio
import pytest

from tests.integration.native_sandbox.matrix import (
    FILE_CHECKS,
    SERVER_CANARY,
    SHELL_CHECKS,
    NativeCall,
    SandboxTree,
)


@pytest.mark.parametrize("check", SHELL_CHECKS)
async def test_shell_boundary(
    check: str, sandbox_tree: SandboxTree, native_call: NativeCall
) -> None:
    probe = sandbox_tree.shell_probe(check)
    # Every negative command must succeed without the tested runtime. A missing
    # executable, broken symlink or enclosing sandbox must not manufacture a pass.
    control = await asyncio.create_subprocess_exec(
        "/bin/sh",
        "-c",
        probe.command,
        cwd=sandbox_tree.workspace,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await control.communicate()
    assert control.returncode == 0, f"invalid control: {stderr.decode()}"
    if probe.written_path is not None:
        assert await anyio.Path(probe.written_path).read_text() == probe.marker
        await anyio.Path(probe.written_path).unlink()
    else:
        assert probe.marker in stdout.decode()
    command = (
        probe.command
        + '\nnative_probe_exit=$?\nprintf "\\nNATIVE_EXIT=%s\\n" "$native_probe_exit"'
    )
    outcome = await native_call("shell", command)
    match = re.search(r"^NATIVE_EXIT=(\d+)$", outcome.text, re.MULTILINE)
    assert match is not None, f"native command did not report its outcome: {outcome}"
    succeeded = match[1] == "0"
    assert succeeded == probe.allowed, (
        f"{check}: expected {'allow' if probe.allowed else 'deny'}; {outcome.text}"
    )
    if probe.allowed:
        assert probe.marker in outcome.text
    if probe.written_path is not None:
        assert not await anyio.Path(probe.written_path).exists()


@pytest.mark.parametrize("check", FILE_CHECKS)
async def test_native_file_tool(
    check: str, sandbox_tree: SandboxTree, native_call: NativeCall
) -> None:
    path = sandbox_tree.file_path(check)
    control = sandbox_tree.workspace / path
    assert await anyio.Path(control).read_text() == sandbox_tree.marker
    outcome = await native_call("file", path)
    if check == "workspace":
        assert not outcome.denied, outcome.text
        assert sandbox_tree.marker in outcome.text
    else:
        assert outcome.denied, f"outside read was not refused: {outcome.text}"
        assert sandbox_tree.marker not in outcome.text


async def test_server_environment_is_hidden(
    native_call: NativeCall,
) -> None:
    assert os.environ[SERVER_CANARY] == "synthetic-server-only"
    outcome = await native_call(
        "shell", f'test -z "${{{SERVER_CANARY}:-}}" && printf NATIVE_ENV_HIDDEN'
    )
    assert "NATIVE_ENV_HIDDEN" in outcome.text, outcome.text
