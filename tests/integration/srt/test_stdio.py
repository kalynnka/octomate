"""Real SRT processes carry scripted harness protocols; no provider calls."""

import asyncio
import json
import os
import sys
from dataclasses import replace
from functools import partial
from pathlib import Path
from unittest.mock import AsyncMock, Mock

import acp
import anyio
import pytest
from acp import schema
from acp.utils import notify_model, request_model
from claude_agent_sdk import (
    ClaudeAgentOptions,
    PermissionResultAllow,
    ToolPermissionContext,
)
from httpx2 import URL
from openai_codex import CodexConfig
from openai_codex.errors import TransportClosedError
from openai_codex.generated.v2_all import ThreadResumeResponse, ThreadStartResponse

from octomate.config.agents import CodexConfig as AgentCodexConfig
from octomate.habitat.srt import SrtHabitat
from octomate.tentacles.acp.client import AcpProvider, NativeAcpClient
from octomate.tentacles.claude.adapter import ClaudeRunAccumulator
from octomate.tentacles.claude.transport import HabitatTransport
from octomate.tentacles.codex import pool as codex_pool
from octomate.tentacles.codex.client import SharedCodex
from octomate.tentacles.codex.ink import CodexInk
from octomate.types.json import JsonObject
from tests.agent.acp.test_client import Callbacks
from tests.habitat.agents.test_claude import options as options
from tests.habitat.agents.test_codex import ProcessInfo

pytestmark = pytest.mark.timeout(60)


async def test_codex_startup_failure_keeps_stderr(
    habitat: SrtHabitat, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    config = CodexConfig(
        cwd=str(tmp_path),
        launch_args_override=(
            "/bin/sh",
            "-c",
            "read ignored; echo srt-codex-diagnostic >&2; exit 7",
        ),
    )
    with pytest.raises(TransportClosedError):
        async with SharedCodex(config, AsyncMock(), instrument=False, habitat=habitat):
            pytest.fail("The failed server must not initialize")
    assert "srt-codex-diagnostic" in caplog.text
    assert not habitat.processes


async def test_codex_pool_reuse_resume_and_peer_cleanup(
    habitat: SrtHabitat, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(codex_pool, "SharedCodex", SharedCodex)
    peer_script = tmp_path / "codex.py"
    peer_script.write_bytes(
        Path(__file__)
        .parents[2]
        .joinpath("agent/codex/fixtures/stdio_sbx.py")
        .read_bytes()
    )
    config = CodexConfig(
        launch_args_override=(
            sys.executable,
            str(peer_script),
            "--debug=false",
            "exec",
            "scripted",
            "codex",
        ),
        env={"CODEX_TEST_LOG": str(tmp_path / "codex.json")},
    )
    ink = CodexInk(
        AgentCodexConfig(runtime=config),
        agent_id="codex",
        mcp_url=URL("http://127.0.0.1:9000/mcp"),
        handler=AsyncMock(return_value={}),
    )
    ink.pool.watch_notifications = partial(
        ink.watch_runtime, invalidate_commands=Mock()
    )
    first, second = tmp_path / "first", tmp_path / "second"
    first.mkdir()
    second.mkdir()
    try:
        clients = await asyncio.gather(
            *(
                ink.pool.acquire(habitat, env={"CODEX_HOME": str(first / ".codex")})
                for _ in range(3)
            )
        )
        client = clients[0]
        assert all(item is client for item in clients)
        peer_config = replace(
            config, cwd=str(second), env={"CODEX_TEST_LOG": str(second / "codex.json")}
        )
        async with SharedCodex(
            peer_config, AsyncMock(return_value={}), instrument=False, habitat=habitat
        ) as peer:
            info = await client._client.request("probe", {}, response_model=ProcessInfo)
            peer_info = await peer._client.request(
                "probe", {}, response_model=ProcessInfo
            )
            assert info.pid != peer_info.pid
            assert info.environment["CODEX_HOME"] == str(first / ".codex")
            started = await client._client.request(
                "thread/start", {}, response_model=ThreadStartResponse
            )
            assert await ink.pool.acquire(thread_id=started.thread.id) is client
            await client.close()
            with pytest.raises(ProcessLookupError):
                os.kill(info.pid, 0)
            assert (
                await peer._client.request("probe", {}, response_model=ProcessInfo)
            ).pid == peer_info.pid
            reopened = await ink.pool.acquire(
                habitat, env={"CODEX_HOME": str(first / ".codex")}
            )
            assert reopened is not client
            resumed = await reopened._client.request(
                "thread/resume",
                {"threadId": started.thread.id},
                response_model=ThreadResumeResponse,
            )
            assert resumed.thread.id == started.thread.id
    finally:
        await ink.close()
        await habitat.resources.aclose()
    assert not habitat.processes


@pytest.mark.parametrize("cancel", [False, True])
async def test_acp_approval_cancel_and_fresh_process_resume(
    habitat: SrtHabitat, tmp_path: Path, cancel: bool
) -> None:
    script = tmp_path / "acp.py"
    script.write_bytes(
        Path(__file__)
        .parents[2]
        .joinpath("agent/acp/fixtures/acp_agent.py")
        .read_bytes()
    )
    log = tmp_path / "acp.jsonl"
    provider = AcpProvider.model_validate(
        {
            "command": sys.executable,
            "args": [str(script)],
            "env": {"ACP_TEST_LOG": str(log)},
        }
    )
    session_id = None
    for _ in range(2):
        callbacks = Callbacks()
        async with NativeAcpClient.connect(
            provider,
            callbacks,
            schema.InitializeRequest(protocol_version=1),
            habitat=habitat,
            cwd=str(tmp_path),
        ) as client:
            if session_id is None:
                session = await client.new_session(
                    schema.NewSessionRequest(cwd=str(tmp_path), mcp_servers=[])
                )
                session_id = session.session_id
            else:
                await request_model(
                    client.connection,
                    "session/load",
                    schema.LoadSessionRequest(
                        session_id=session_id, cwd=str(tmp_path), mcp_servers=[]
                    ),
                    schema.LoadSessionResponse,
                )
            prompt = asyncio.create_task(
                request_model(
                    client.connection,
                    "session/prompt",
                    schema.PromptRequest(
                        session_id=session_id, prompt=[acp.text_block("hello")]
                    ),
                    schema.PromptResponse,
                )
            )
            await callbacks.asked.wait()
            assert callbacks.updates
            if cancel:
                await notify_model(
                    client.connection,
                    "session/cancel",
                    schema.CancelNotification(session_id=session_id),
                )
            else:
                callbacks.answered.set()
            assert (await prompt).stop_reason == ("cancelled" if cancel else "end_turn")
        records = [json.loads(line) for line in log.read_text().splitlines()]
        with pytest.raises(ProcessLookupError):
            os.kill(records[0]["pid"], 0)
    assert not habitat.processes


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
