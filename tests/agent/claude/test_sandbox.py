"""A real SDK client drives a scripted CLI through sbx's duplex attachment."""

import asyncio
import os
import sys
from pathlib import Path
from unittest.mock import AsyncMock

import anyio
import pytest
from anyio.abc import ByteStream
from claude_agent_sdk import (
    ClaudeAgentOptions,
    PermissionResultAllow,
    ResultMessage,
    ToolPermissionContext,
)
from pydantic import BaseModel
from pydantic_ai.messages import PartDeltaEvent
from uuid_utils.compat import uuid7

from octomate.config.sandbox import SbxSandboxConfig
from octomate.fibers import FiberError
from octomate.managers.sandbox import SandboxManager
from octomate.nerve import Nerve
from octomate.schemas.sandbox import InvocationScope
from octomate.tentacles.claude.adapter import ClaudeRunAccumulator
from octomate.tentacles.claude.transport import SandboxTransport
from octomate.types.json import JsonObject
from tests.test_sandbox_contracts import IMAGE


class ProcessInfo(BaseModel):
    pid: int
    argv: list[str]


@pytest.fixture
def options(tmp_path: Path) -> ClaudeAgentOptions:
    cli = tmp_path / "claude"
    cli.write_text(
        f"#!{sys.executable}\n"
        + Path(__file__).with_name("fixtures").joinpath("stdio_cli.py").read_text()
    )
    cli.chmod(0o700)
    return ClaudeAgentOptions(
        cli_path=cli,
        cwd=tmp_path,
        model="scripted",
        include_partial_messages=True,
        env={"CLAUDE_TEST_LOG": str(tmp_path / "protocol.jsonl")},
    )


@pytest.fixture
def manager(tmp_path: Path) -> SandboxManager:
    executable = tmp_path / "sbx"
    executable.write_text(
        f"#!{sys.executable}\n"
        "import os, sys\n"
        "args = sys.argv[1:]\n"
        "assert args[:2] == ['--debug=false', 'exec']\n"
        "assert not os.isatty(0) and not os.isatty(1)\n"
        "assert 'OCTOMATE_DB_URL' not in os.environ\n"
        "assert 'ANTHROPIC_API_KEY' not in os.environ\n"
        "index = 2\n"
        "while args[index].startswith('--'):\n"
        "    key, _, value = args[index].partition('=')\n"
        "    if key == '--env':\n"
        "        name, _, content = value.partition('=')\n"
        "        os.environ[name] = content\n"
        "    elif key == '--workdir':\n"
        "        os.chdir(value)\n"
        "    index += 1\n"
        "command = args[index + 1:]\n"
        "os.execvpe(command[0], command, os.environ)\n"
    )
    executable.chmod(0o700)
    return SandboxManager(
        [SbxSandboxConfig(id="local", executable=str(executable), image=IMAGE)],
        Nerve(),
    )


@pytest.fixture
def scope(manager: SandboxManager, monkeypatch: pytest.MonkeyPatch) -> InvocationScope:
    scope = InvocationScope(user_id=uuid7(), sandbox="local", conversation_id=uuid7())
    sandbox = manager.state(scope).sandbox
    monkeypatch.setattr(sandbox, "prepare", AsyncMock())
    monkeypatch.setattr(sandbox, "stop", AsyncMock())
    return scope


async def test_native_events_approval_and_controls_share_the_connection(
    manager: SandboxManager,
    scope: InvocationScope,
    options: ClaudeAgentOptions,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "must-stay-on-host")
    asked, answer = asyncio.Event(), asyncio.Event()

    async def approve(
        tool: str, arguments: JsonObject, context: ToolPermissionContext
    ) -> PermissionResultAllow:
        assert tool == "Bash"
        assert context.tool_use_id == "tool-1"
        assert arguments == {"command": "printf approved"}
        asked.set()
        await answer.wait()
        return PermissionResultAllow(updated_input=arguments)

    options.can_use_tool = approve
    accumulator = ClaudeRunAccumulator()
    accumulator.begin("Run the tool")
    deltas: list[str] = []
    try:
        async with SandboxTransport.client(manager, scope, options) as client:
            assert len(manager.state(scope).invocations) == 1
            assert await client.get_server_info() == {"commands": [], "models": []}
            await client.query("Run the tool")
            async with asyncio.timeout(5):
                await asked.wait()
                await client.set_permission_mode("default")
                answer.set()
                async for message in client.receive_response():
                    for event in accumulator.consume(message):
                        if isinstance(event, PartDeltaEvent):
                            deltas.append(str(event.delta))
            assert accumulator.result_text == "Approved"
            assert deltas
    finally:
        await manager.close()
    assert not manager.state(scope).invocations
    log = await anyio.Path(options.env["CLAUDE_TEST_LOG"]).read_text()
    info = ProcessInfo.model_validate_json(log.splitlines()[0])
    assert "--permission-prompt-tool" in info.argv
    assert options.permission_prompt_tool_name is None
    assert '"type": "control_response"' in log
    with pytest.raises(ProcessLookupError):
        os.kill(info.pid, 0)


@pytest.mark.parametrize(
    ("frame", "reason"),
    [
        (b'{"token": "secret"}\n', "size limit"),
        (b'"secret"\n', "Invalid"),
        (b"secret", "Incomplete"),
        (b"\xff\n", "Invalid"),
    ],
)
async def test_invalid_frames_fail_without_echoing_payloads(
    frame: bytes, reason: str
) -> None:
    stream = AsyncMock(spec=ByteStream)
    stream.receive.side_effect = [frame, anyio.EndOfStream]
    transport = SandboxTransport(stream, max_frame_bytes=10)
    with pytest.raises(FiberError, match=reason) as error:
        await anext(transport.read_messages())
    assert "secret" not in str(error.value)
    await transport.close()
    stream.aclose.assert_awaited_once()


async def test_initialization_failure_releases_process_and_lease(
    manager: SandboxManager, scope: InvocationScope, options: ClaudeAgentOptions
) -> None:
    options.env["CLAUDE_TEST_FAIL_INIT"] = "1"
    try:
        async with asyncio.timeout(5):
            with pytest.raises(FiberError, match="stream closed"):
                async with SandboxTransport.client(manager, scope, options):
                    pytest.fail("The CLI exited before initialization")
    finally:
        await manager.close()
    assert not manager.state(scope).invocations
    info = ProcessInfo.model_validate_json(
        (await anyio.Path(options.env["CLAUDE_TEST_LOG"]).read_text()).splitlines()[0]
    )
    with pytest.raises(ProcessLookupError):
        os.kill(info.pid, 0)


async def test_end_input_allows_the_peer_to_finish_its_output() -> None:
    stream = AsyncMock(spec=ByteStream)
    stream.receive.side_effect = [b'{"type":"result"}\n', anyio.EndOfStream]
    transport = SandboxTransport(stream)
    await transport.connect()
    assert transport.is_ready()
    await transport.end_input()
    stream.send_eof.assert_awaited_once()
    assert [frame async for frame in transport.read_messages()] == [{"type": "result"}]
    await transport.close()
    assert not transport.is_ready()


@pytest.mark.parametrize("cancel_task", [False, True])
async def test_interrupt_and_cancellation_release_process_and_lease(
    manager: SandboxManager,
    scope: InvocationScope,
    options: ClaudeAgentOptions,
    cancel_task: bool,
) -> None:
    asked = asyncio.Event()

    async def approve(
        tool: str, arguments: JsonObject, context: ToolPermissionContext
    ) -> PermissionResultAllow:
        asked.set()
        await asyncio.Future[None]()
        return PermissionResultAllow()

    options.can_use_tool = approve

    async def run() -> None:
        async with SandboxTransport.client(manager, scope, options) as client:
            await client.query("Wait for approval")
            await asked.wait()
            if cancel_task:
                await asyncio.Future[None]()
            await client.interrupt()
            messages = [message async for message in client.receive_response()]
            assert isinstance(messages[-1], ResultMessage)
            assert messages[-1].result == "Interrupted"

    try:
        async with asyncio.timeout(5), asyncio.TaskGroup() as tasks:
            task = tasks.create_task(run())
            await asked.wait()
            if cancel_task:
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
            else:
                await task
    finally:
        await manager.close()
    assert not manager.state(scope).invocations
    info = ProcessInfo.model_validate_json(
        (await anyio.Path(options.env["CLAUDE_TEST_LOG"]).read_text()).splitlines()[0]
    )
    with pytest.raises(ProcessLookupError):
        os.kill(info.pid, 0)
