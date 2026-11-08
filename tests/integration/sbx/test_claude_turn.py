"""Opt-in model calls through the core SDK and an actual guest Claude CLI."""

import asyncio
import os
from collections.abc import AsyncGenerator
from dataclasses import replace

import pytest
from claude_agent_sdk import (
    ClaudeAgentOptions,
    ClaudeSDKClient,
    PermissionResultAllow,
    PermissionResultDeny,
    ResultMessage,
    ToolPermissionContext,
)
from pydantic import TypeAdapter
from pydantic_ai.messages import PartDeltaEvent
from uuid_utils.compat import uuid7

from octomate.config.sandbox import SbxSandboxConfig
from octomate.managers.sandbox import SandboxManager
from octomate.nerve import Nerve
from octomate.sandbox.sbx import SbxSandbox
from octomate.schemas.sandbox import InvocationScope
from octomate.tentacles.claude.adapter import ClaudeRunAccumulator
from octomate.tentacles.claude.transport import SandboxTransport
from octomate.types.json import JsonObject

pytestmark = [
    pytest.mark.timeout(600),
    pytest.mark.skipif(
        os.environ.get("SBX_TEST_CLAUDE_TURN") != "1",
        reason="Set SBX_TEST_CLAUDE_TURN=1 to use the sbx Claude credential for model calls",
    ),
]


@pytest.fixture
async def guest() -> AsyncGenerator[tuple[SandboxManager, InvocationScope, SbxSandbox]]:
    image = os.environ.get("SBX_TEST_IMAGE")
    if not image:
        pytest.fail("Set SBX_TEST_IMAGE to the immutable SDK image reference")
    manager = SandboxManager([SbxSandboxConfig(id="local", image=image)], Nerve())
    scope = InvocationScope(user_id=uuid7(), sandbox="local", conversation_id=uuid7())
    sandbox = manager.state(scope).sandbox
    assert isinstance(sandbox, SbxSandbox)
    assert (
        sandbox.name
        not in (await sandbox.command("ls", quiet=True)).decode().splitlines()
    )
    try:
        async with manager.acquire(scope, uuid7()):
            await sandbox.command(
                "exec",
                sandbox.name,
                "/opt/octomate/.venv/bin/python",
                "-c",
                "import json; from pathlib import Path; "
                "oauth = json.loads(Path('/home/agent/.claude/.credentials.json').read_text())['claudeAiOauth']; "
                "assert oauth['accessToken'] == 'sk-ant-oat01-proxy-managed'; "
                "assert oauth['refreshToken'] == 'sk-ant-ort01-proxy-managed'",
            )
        yield manager, scope, sandbox
    finally:
        try:
            await manager.close()
        finally:
            if (
                sandbox.name
                in (await sandbox.command("ls", quiet=True)).decode().splitlines()
            ):
                await sandbox.command("rm", "--force", sandbox.name)


async def turn(client: ClaudeSDKClient, prompt: str) -> ClaudeRunAccumulator:
    accumulator = ClaudeRunAccumulator()
    accumulator.begin(prompt)
    streamed = False
    terminal: ResultMessage | None = None
    await client.query(prompt)
    async for message in client.receive_response():
        for event in accumulator.consume(message):
            streamed |= isinstance(event, PartDeltaEvent)
        if isinstance(message, ResultMessage):
            terminal = message
    assert terminal is not None, "Claude ended without a result"
    assert not terminal.is_error, terminal.result
    assert streamed, "No native stream events were translated in core"
    return accumulator


async def cli_pids(sandbox: SbxSandbox) -> list[int]:
    return TypeAdapter(list[int]).validate_json(
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


async def test_model_tools_resume_interrupt_and_cancel(
    guest: tuple[SandboxManager, InvocationScope, SbxSandbox],
) -> None:
    manager, scope, sandbox = guest
    token = uuid7().hex
    command = f"printf '%s' '{token}' > transport-proof.txt"
    asked, answer = asyncio.Event(), asyncio.Event()
    approvals: list[str] = []

    async def approve(
        tool: str,
        arguments: JsonObject,
        context: ToolPermissionContext,
    ) -> PermissionResultAllow | PermissionResultDeny:
        if tool != "Bash" or arguments.get("command") != command:
            return PermissionResultDeny(
                message=f"Use exactly this Bash command: {command}"
            )
        assert context.tool_use_id
        approvals.append(command)
        asked.set()
        await answer.wait()
        return PermissionResultAllow(updated_input=arguments)

    options = ClaudeAgentOptions(
        cli_path="/usr/local/bin/claude",
        cwd="/home/agent/workspace",
        model=os.environ.get("SBX_TEST_CLAUDE_MODEL", "haiku"),
        tools=["Bash"],
        can_use_tool=approve,
        permission_mode="default",
        setting_sources=[],
        strict_mcp_config=True,
        include_partial_messages=True,
        max_turns=4,
        max_budget_usd=0.5,
        session_id=str(uuid7()),
    )
    prompt = f"Use Bash to run exactly: {command}\nThen reply with the token you wrote."
    async with SandboxTransport.client(manager, scope, options) as client:
        async with asyncio.TaskGroup() as tasks:
            response = tasks.create_task(turn(client, prompt))
            async with asyncio.timeout(120):
                await asked.wait()
            assert (
                await sandbox.command(
                    "exec",
                    sandbox.name,
                    "/opt/octomate/.venv/bin/python",
                    "-c",
                    "from pathlib import Path; print(Path('/home/agent/workspace/transport-proof.txt').exists())",
                )
                == b"False\n"
            )
            await client.set_permission_mode("default")
            answer.set()
        assert token in response.result().result_text
        assert response.result().session_id == options.session_id
        assert approvals == [command]
        assert (
            await sandbox.command(
                "exec",
                sandbox.name,
                "cat",
                "/home/agent/workspace/transport-proof.txt",
            )
            == token.encode()
        )
    assert not manager.state(scope).invocations
    assert await cli_pids(sandbox) == []

    resumed = replace(options, session_id=None, resume=options.session_id, tools=[])
    async with SandboxTransport.client(manager, scope, resumed) as client:
        result = await turn(
            client,
            "What token did you write in the previous turn? Reply with only that token; do not use tools.",
        )
        assert token in result.result_text
        assert result.session_id == options.session_id

    interrupt = asyncio.Event()

    async def pending(fresh: ClaudeAgentOptions) -> None:
        async with SandboxTransport.client(manager, scope, fresh) as client:
            await client.query(prompt)
            await interrupt.wait()
            await client.interrupt()
            results = [
                message
                async for message in client.receive_response()
                if isinstance(message, ResultMessage)
            ]
            assert results, "Interrupt did not finish the turn"

    for cancel in (False, True):
        asked.clear()
        answer.clear()
        interrupt.clear()
        fresh = replace(options, session_id=str(uuid7()))

        async with asyncio.TaskGroup() as tasks:
            pending_turn = tasks.create_task(pending(fresh))
            async with asyncio.timeout(120):
                await asked.wait()
            assert await cli_pids(sandbox), "Expected a live guest CLI"
            if cancel:
                pending_turn.cancel()
            else:
                interrupt.set()
        assert pending_turn.cancelled() is cancel
        assert not manager.state(scope).invocations
        assert await cli_pids(sandbox) == []
