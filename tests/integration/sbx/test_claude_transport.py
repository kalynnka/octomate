"""The core SDK controls an actual guest CLI without submitting a model request."""

import os

import pytest
from claude_agent_sdk import ClaudeAgentOptions
from pydantic import TypeAdapter
from uuid_utils.compat import uuid7

from octomate.config.habitat import SbxHabitatConfig
from octomate.habitat.sbx import SbxHabitat
from octomate.managers.habitat import HabitatManager
from octomate.nerve import Nerve
from octomate.schemas.habitat import InvocationScope
from octomate.tentacles.claude.transport import HabitatTransport
from tests.support.managers import a_workspace_manager


@pytest.mark.timeout(600)
async def test_guest_cli_controls_and_process_cleanup() -> None:
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
    try:
        async with HabitatTransport.client(manager, scope, options) as client:
            assert len(state.invocations) == 1
            assert await client.get_server_info() is not None
            await client.set_permission_mode("default")
            await client.interrupt()
            pids = TypeAdapter(list[int]).validate_json(
                await sandbox.command(
                    "exec",
                    sandbox.name,
                    "/opt/octomate/.venv/bin/python",
                    "-c",
                    "import json; from pathlib import Path; "
                    "print(json.dumps([int(p.name) for p in Path('/proc').iterdir() "
                    "if p.name.isdigit() and (p/'cmdline').exists() and "
                    "b'--output-format' in (p/'cmdline').read_bytes().split(b'\\0')]))",
                )
            )
            assert pids, "No guest CLI process found"
        assert not state.invocations
        assert state.status.phase == "ready"
        await sandbox.command(
            "exec",
            sandbox.name,
            "/opt/octomate/.venv/bin/python",
            "-c",
            "import sys,time; from pathlib import Path\n"
            "pids = [int(pid) for pid in sys.argv[1:]]\n"
            "deadline = time.monotonic() + 5\n"
            "while time.monotonic() < deadline:\n"
            "    if not any(Path(f'/proc/{pid}').exists() for pid in pids): break\n"
            "    time.sleep(0.05)\n"
            "assert not any(Path(f'/proc/{pid}').exists() for pid in pids), 'Guest CLI survived disconnect'",
            *(str(pid) for pid in pids),
        )
    finally:
        try:
            await manager.close()
        finally:
            if (
                sandbox.name
                in (await sandbox.command("ls", quiet=True)).decode().splitlines()
            ):
                await sandbox.command("rm", "--force", sandbox.name)
