"""The core Codex SDK exchanges native RPC with an actual guest app-server."""

import asyncio
import os
from contextlib import AsyncExitStack
from dataclasses import replace
from unittest.mock import AsyncMock

import pytest
from openai_codex import CodexConfig
from openai_codex.errors import TransportClosedError
from openai_codex.generated.v2_all import ThreadStartParams
from uuid_utils.compat import uuid7

from octomate.config.habitat import SbxHabitatConfig
from octomate.habitat.sbx import SbxHabitat
from octomate.managers.habitat import HabitatManager
from octomate.nerve import Nerve
from octomate.schemas.habitat import InvocationScope
from octomate.tentacles.codex.client import SharedCodex
from tests.integration.sbx import cli_pids, remove_sandbox
from tests.support.managers import a_workspace_manager


@pytest.mark.timeout(600)
async def test_guest_codex_rpc_notifications_stderr_and_cleanup(
    sbx_cleanup: AsyncExitStack, caplog: pytest.LogCaptureFixture
) -> None:
    image = os.environ.get("SBX_TEST_IMAGE")
    if not image:
        pytest.fail("Set SBX_TEST_IMAGE to the immutable SDK image reference")
    manager = HabitatManager(
        SbxHabitatConfig(image=image), Nerve(), a_workspace_manager()
    )
    scope = InvocationScope(user_id=uuid7(), conversation_id=uuid7())
    sandbox = manager.residence(scope).habitat
    assert isinstance(sandbox, SbxHabitat)
    assert (
        sandbox.name
        not in (await sandbox.command("ls", quiet=True)).decode().splitlines()
    )
    sbx_cleanup.push_async_callback(remove_sandbox, sandbox)
    sbx_cleanup.push_async_callback(manager.close)
    config = CodexConfig(
        codex_bin="/usr/local/bin/codex",
        cwd="/home/agent/workspace",
        env={"CODEX_HOME": "/home/agent/.codex"},
    )
    handler = AsyncMock(return_value={"decision": "decline"})
    client = SharedCodex(config, handler, instrument=True, habitat=sandbox)
    async with manager.enter(scope, uuid7()):
        await sandbox.command("exec", sandbox.name, "mkdir", "-p", "/home/agent/.codex")
        await client.start()
        assert client.metadata.userAgent
        started = await client._client.thread_start(
            ThreadStartParams(cwd=config.cwd, ephemeral=True)
        )
        assert started.thread.cwd.root == config.cwd
        async with asyncio.timeout(15):
            notification = await client._client.next_notification()
            while notification.method != "thread/started":
                notification = await client._client.next_notification()
        pids = await cli_pids(sandbox, argument_pattern="app-serve[r]")
        assert len(pids) == 1, "Expected one guest Codex app-server"
    assert not manager.residence(scope).invocations
    assert client.transport.reader_thread is not None
    assert client.transport.reader_thread.is_alive()
    peer = scope.model_copy(update={"conversation_id": uuid7()})
    async with manager.enter(peer, uuid7()):
        await asyncio.gather(*(client.start() for _ in range(4)))
        reads = await asyncio.gather(
            *(client._client.thread_read(started.thread.id) for _ in range(4))
        )
        assert {read.thread.id for read in reads} == {started.thread.id}
        assert await cli_pids(sandbox, argument_pattern="app-serve[r]") == pids
        await client.close()
        assert not client.transport.reader_thread.is_alive()
        await sandbox.command(
            "exec",
            sandbox.name,
            "timeout",
            "5",
            "sh",
            "-c",
            'for pid do while test -d "/proc/$pid"; do sleep 0.05; done; done',
            "sh",
            *(str(pid) for pid in pids),
        )
        failed = replace(
            config,
            launch_args_override=(
                "sh",
                "-c",
                "read ignored; echo codex-transport-diagnostic >&2; exit 7",
            ),
        )
        with pytest.raises(TransportClosedError):
            async with SharedCodex(failed, handler, instrument=False, habitat=sandbox):
                pytest.fail("The guest should fail during initialization")
        assert "codex-transport-diagnostic" in caplog.text
    assert not manager.residence(scope).invocations
    handler.assert_not_called()
