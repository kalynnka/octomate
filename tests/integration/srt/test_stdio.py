"""Real SRT processes carry scripted harness protocols; no provider calls."""

import asyncio
import json
import os

import anyio
import pytest
from claude_agent_sdk import (
    ClaudeAgentOptions,
    PermissionResultAllow,
    ToolPermissionContext,
)

from octomate.habitat.srt import SrtHabitat
from octomate.tentacles.claude.adapter import ClaudeRunAccumulator
from octomate.tentacles.claude.transport import HabitatTransport
from octomate.types.json import JsonObject
from tests.habitat.agents.test_claude import options as options

pytestmark = pytest.mark.timeout(60)


async def test_claude_controls_approval_and_turn_cleanup(
    habitat: SrtHabitat, options: ClaudeAgentOptions
) -> None:
    asked, answer = asyncio.Event(), asyncio.Event()

    async def approve(
        tool: str, arguments: JsonObject, context: ToolPermissionContext
    ) -> PermissionResultAllow:
        assert tool == "Bash"
        assert context.tool_use_id == "tool-1"
        asked.set()
        await answer.wait()
        return PermissionResultAllow(updated_input=arguments)

    options.can_use_tool = approve
    accumulator = ClaudeRunAccumulator()
    accumulator.begin("Run the tool")
    async with HabitatTransport.client(habitat, options) as client:
        assert await client.get_server_info() == {"commands": [], "models": []}
        await client.query("Run the tool")
        await asked.wait()
        await client.set_permission_mode("default")
        answer.set()
        async for message in client.receive_response():
            list(accumulator.consume(message))
        assert accumulator.result_text == "Approved"
    info = json.loads(
        (await anyio.Path(options.env["CLAUDE_TEST_LOG"]).read_text()).splitlines()[0]
    )
    with pytest.raises(ProcessLookupError):
        os.kill(info["pid"], 0)
    assert not habitat.processes
