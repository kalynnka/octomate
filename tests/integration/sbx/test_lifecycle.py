"""Explicit local VM acceptance with disposable identities and no host mounts."""

import asyncio
import os

import pytest
from uuid_utils.compat import uuid7

from octomate.config.sandbox import SbxSandboxConfig
from octomate.managers.sandbox import SandboxManager
from octomate.nerve import Nerve
from octomate.sandbox.sbx import SbxSandbox
from octomate.schemas.sandbox import InvocationScope


@pytest.mark.timeout(600)
async def test_sandbox_agent_attachment_reuse_and_stop_start_persistence() -> None:
    image = os.environ.get("SBX_TEST_IMAGE")
    if not image:
        pytest.fail(
            "Set SBX_TEST_IMAGE to the immutable, built sandbox agent image reference"
        )
    manager = SandboxManager(SbxSandboxConfig(image=image), Nerve())
    owner = InvocationScope(user_id=uuid7(), mode="sbx", conversation_id=uuid7())
    other = InvocationScope(user_id=uuid7(), mode="sbx", conversation_id=uuid7())
    sandbox = manager.state(owner.user_id).sandbox
    assert isinstance(sandbox, SbxSandbox)
    existing = (await sandbox.command("ls", "--quiet")).decode().splitlines()
    assert manager.state(owner.user_id).sandbox.name not in existing
    assert manager.state(other.user_id).sandbox.name not in existing
    try:
        async with manager.acquire(owner, uuid7()) as first:
            async with manager.acquire(owner, uuid7()) as peer:
                assert first is peer
                assert first.ready is not None
                assert not first.ready.accepts_invocations
                assert len(first.invocations) == 2
            async with manager.acquire(other, uuid7()) as second:
                assert first.sandbox.name != second.sandbox.name
                assert sandbox.process is not None
                assert sandbox.process.is_alive()
            await asyncio.sleep(35)
            assert sandbox.process is not None
            assert sandbox.process.is_alive()
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
        await manager.stop(owner.user_id)
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
                await backend.command("rm", backend.name)
