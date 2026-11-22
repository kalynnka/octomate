"""The core SDK controls an actual guest CLI without submitting a model request."""

import os
from contextlib import AsyncExitStack, suppress

import anyio
import pytest
from claude_agent_sdk import ClaudeAgentOptions
from uuid_utils.compat import uuid7

from octomate.config.habitat import SbxHabitatConfig
from octomate.habitat.base import HabitatError
from octomate.habitat.sbx import SbxHabitat
from octomate.managers.habitat import HabitatManager
from octomate.nerve import Nerve
from octomate.schemas.habitat import InvocationScope
from octomate.tentacles.claude.transport import HabitatTransport
from tests.integration.sbx import cli_pids, remove_sandbox
from tests.support.managers import a_workspace_manager


@pytest.mark.timeout(600)
async def test_guest_cli_controls_and_process_cleanup(
    sbx_cleanup: AsyncExitStack, caplog: pytest.LogCaptureFixture
) -> None:
    image = os.environ.get("SBX_TEST_IMAGE")
    if not image:
        pytest.fail("Set SBX_TEST_IMAGE to the immutable SDK image reference")
    manager = HabitatManager(
        SbxHabitatConfig(image=image), Nerve(), a_workspace_manager()
    )
    scope = InvocationScope(user_id=uuid7(), conversation_id=uuid7())
    state = manager.residence(scope)
    sandbox = state.habitat
    assert isinstance(sandbox, SbxHabitat)
    assert (
        sandbox.name
        not in (await sandbox.command("ls", quiet=True)).decode().splitlines()
    )
    options = ClaudeAgentOptions(
        cli_path="/usr/local/bin/claude",
        cwd="/home/agent/workspace",
        setting_sources=[],
    )
    sbx_cleanup.push_async_callback(remove_sandbox, sandbox)
    sbx_cleanup.push_async_callback(manager.close)
    async with (
        manager.enter(scope, uuid7()),
        HabitatTransport.client(sandbox, options) as client,
    ):
        assert len(state.invocations) == 1
        assert await client.get_server_info() is not None
        await client.set_permission_mode("default")
        await client.interrupt()
        pids = await cli_pids(sandbox)
        assert pids, "No guest CLI process found"
    assert not state.invocations
    assert state.status.phase == "ready"
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
    stderr: list[str] = []
    # The error is raised on context exit after guest stdio has drained.
    with pytest.raises(HabitatError, match="exit 7"):  # noqa: PT012
        async with sandbox.execute(
            "sh",
            "-c",
            "echo acceptance-diagnostic >&2; exit 7",
            workdir="/home/agent/workspace",
            stderr=stderr.append,
        ) as stream:
            await stream.send_eof()
            with suppress(anyio.EndOfStream):
                await stream.receive()
    assert "acceptance-diagnostic" in stderr
    assert "acceptance-diagnostic" in caplog.text
