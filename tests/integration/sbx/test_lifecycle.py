"""Explicit local VM acceptance with disposable identities and no host mounts."""

import os

import pytest
from anyio.streams.buffered import BufferedByteReceiveStream
from uuid_utils.compat import uuid7

from octomate.config.sandbox import SbxSandboxConfig
from octomate.managers.sandbox import SandboxManager
from octomate.nerve import Nerve
from octomate.sandbox.sbx import SbxSandbox
from octomate.schemas.sandbox import InvocationScope


@pytest.mark.timeout(600)
async def test_command_attachment_reuse_and_stop_start_persistence() -> None:
    image = os.environ.get("SBX_TEST_IMAGE")
    if not image:
        pytest.fail("Set SBX_TEST_IMAGE to the immutable SDK image reference")
    manager = SandboxManager(SbxSandboxConfig(image=image), Nerve())
    owner = InvocationScope(user_id=uuid7(), conversation_id=uuid7())
    other = InvocationScope(user_id=uuid7(), conversation_id=uuid7())
    sandbox = manager.state(owner).sandbox
    assert isinstance(sandbox, SbxSandbox)
    existing = (await sandbox.command("ls", "--quiet")).decode().splitlines()
    assert manager.state(owner).sandbox.name not in existing
    assert manager.state(other).sandbox.name not in existing
    try:
        async with manager.acquire(owner, uuid7()) as first:
            async with sandbox.execute(
                "cat", workdir="/home/agent/workspace"
            ) as stream:
                await stream.send(b"ready\n")
                assert (
                    await BufferedByteReceiveStream(stream).receive_until(b"\n", 64)
                    == b"ready"
                )
                await stream.send_eof()
            assert len(first.invocations) == 1
            async with manager.acquire(owner, uuid7()) as peer:
                assert first is peer
                assert first.snapshot.phase == "ready"
                assert len(first.invocations) == 2
            async with manager.acquire(other, uuid7()) as second:
                assert first.sandbox.name != second.sandbox.name
            await sandbox.command(
                "exec",
                "--workdir",
                "/home/agent/workspace",
                first.sandbox.name,
                "/opt/octomate/.venv/bin/python",
                "-c",
                "from pathlib import Path; "
                "Path('uncommitted.txt').write_text('retained'); "
                "tool = Path('/home/agent/custom-tool'); "
                "tool.write_text('#!/bin/sh\\nprintf installed'); tool.chmod(0o755); "
                "Path('/home/agent/session-sentinel').write_text('session')",
            )
        assert not first.invocations
        await manager.stop(manager.state(owner))
        assert manager.snapshot(owner).phase == "stopped"
        async with manager.acquire(owner, uuid7()) as restarted:
            output = await sandbox.command(
                "exec",
                "--workdir",
                "/home/agent/workspace",
                restarted.sandbox.name,
                "/opt/octomate/.venv/bin/python",
                "-c",
                "from pathlib import Path; "
                "assert Path('uncommitted.txt').read_text() == 'retained'; "
                "assert Path('/home/agent/custom-tool').stat().st_mode & 0o111; "
                "assert Path('/home/agent/session-sentinel').read_text() == 'session'; "
                "print('persistent')",
            )
            assert output.strip() == b"persistent"
            assert (
                await sandbox.command(
                    "exec", restarted.sandbox.name, "/home/agent/custom-tool"
                )
            ).strip() == b"installed"
    finally:
        await manager.close()
        for state in manager.sandboxes.values():
            backend = state.sandbox
            assert isinstance(backend, SbxSandbox)
            if backend.managed:
                await backend.command("rm", "--force", backend.name)
