"""A real SDK client drives a scripted CLI through sbx's duplex attachment."""

import asyncio
import json
import os
import sys
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
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
from pydantic_ai import AgentRunResultEvent
from pydantic_ai.messages import PartDeltaEvent
from sqlalchemy.ext.asyncio import AsyncEngine
from uuid_utils.compat import uuid7

from octomate import Octomate
from octomate.config import OctomateConfig
from octomate.config.agents import ClaudeCodeConfig
from octomate.config.habitat import SbxHabitatConfig
from octomate.fibers import FiberError
from octomate.habitat.sbx import SbxHabitat
from octomate.managers.habitat import HabitatManager
from octomate.nerve import Nerve
from octomate.schemas.conversation import ChannelAddress
from octomate.schemas.habitat import InvocationScope
from octomate.tentacles.claude import ClaudeTentacle
from octomate.tentacles.claude.adapter import ClaudeRunAccumulator
from octomate.tentacles.claude.transport import HabitatTransport
from octomate.types.json import JsonObject
from tests.support.invocations import invocation_for
from tests.support.managers import (
    FakeConversationManager,
    a_thread,
    a_workspace_manager,
)
from tests.support.users import a_user, auth_config
from tests.test_habitat_contracts import IMAGE


class ProcessInfo(BaseModel):
    pid: int
    argv: list[str]
    cwd: str
    config: str | None


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
def executable(tmp_path: Path) -> Path:
    """A stand-in sbx that runs the guest command as a host process."""
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
    return executable


@pytest.fixture
def manager(executable: Path) -> HabitatManager:
    return HabitatManager(
        SbxHabitatConfig(executable=str(executable), image=IMAGE),
        Nerve(),
        a_workspace_manager(),
    )


@pytest.fixture
def scope(manager: HabitatManager, monkeypatch: pytest.MonkeyPatch) -> InvocationScope:
    scope = InvocationScope(user_id=uuid7(), conversation_id=uuid7())
    habitat = manager.residence(scope).habitat
    monkeypatch.setattr(habitat, "prepare", AsyncMock())
    monkeypatch.setattr(habitat, "stop", AsyncMock())
    return scope


async def test_native_events_approval_and_controls_share_the_connection(
    manager: HabitatManager,
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
        async with (
            manager.enter(scope, uuid7()) as residence,
            HabitatTransport.client(residence.habitat, options) as client,
        ):
            assert len(residence.invocations) == 1
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
    assert not manager.residence(scope).invocations
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
    transport = HabitatTransport(stream, max_frame_bytes=10)
    with pytest.raises(FiberError, match=reason) as error:
        await anext(transport.read_messages())
    assert "secret" not in str(error.value)
    await transport.close()
    stream.aclose.assert_awaited_once()


async def test_initialization_failure_releases_process_and_lease(
    manager: HabitatManager, scope: InvocationScope, options: ClaudeAgentOptions
) -> None:
    options.env["CLAUDE_TEST_FAIL_INIT"] = "1"
    try:
        async with asyncio.timeout(5):
            with pytest.raises(FiberError, match="stream closed"):
                async with (
                    manager.enter(scope, uuid7()) as residence,
                    HabitatTransport.client(residence.habitat, options),
                ):
                    pytest.fail("The CLI exited before initialization")
    finally:
        await manager.close()
    assert not manager.residence(scope).invocations
    info = ProcessInfo.model_validate_json(
        (await anyio.Path(options.env["CLAUDE_TEST_LOG"]).read_text()).splitlines()[0]
    )
    with pytest.raises(ProcessLookupError):
        os.kill(info.pid, 0)


async def test_end_input_allows_the_peer_to_finish_its_output() -> None:
    stream = AsyncMock(spec=ByteStream)
    stream.receive.side_effect = [b'{"type":"result"}\n', anyio.EndOfStream]
    transport = HabitatTransport(stream)
    await transport.connect()
    assert transport.is_ready()
    await transport.end_input()
    stream.send_eof.assert_awaited_once()
    assert [frame async for frame in transport.read_messages()] == [{"type": "result"}]
    await transport.close()
    assert not transport.is_ready()


@pytest.mark.parametrize("cancel_task", [False, True])
async def test_client_exit_waits_for_reader_completion_and_remains_cancellable(
    manager: HabitatManager,
    scope: InvocationScope,
    options: ClaudeAgentOptions,
    monkeypatch: pytest.MonkeyPatch,
    cancel_task: bool,
) -> None:
    at_eof, finish, reader_stopped = asyncio.Event(), asyncio.Event(), asyncio.Event()
    cancel_scope = anyio.CancelScope()
    read_messages = HabitatTransport.read_messages

    async def read_until_released(
        transport: HabitatTransport,
    ) -> AsyncGenerator[JsonObject]:
        try:
            async for message in read_messages(transport):
                yield message
            at_eof.set()
            await finish.wait()
        finally:
            reader_stopped.set()

    monkeypatch.setattr(HabitatTransport, "read_messages", read_until_released)

    async def run() -> None:
        with cancel_scope:
            async with (
                manager.enter(scope, uuid7()) as residence,
                HabitatTransport.client(residence.habitat, options) as client,
            ):
                assert await client.get_server_info() == {"commands": [], "models": []}

    try:
        async with asyncio.timeout(5), asyncio.TaskGroup() as tasks:
            task = tasks.create_task(run())
            await at_eof.wait()
            assert not task.done()
            assert not reader_stopped.is_set()
            assert len(manager.residence(scope).invocations) == 1
            if cancel_task:
                cancel_scope.cancel()
            else:
                finish.set()
            await task
    finally:
        await manager.close()
    assert reader_stopped.is_set()
    assert not manager.residence(scope).invocations
    info = ProcessInfo.model_validate_json(
        (await anyio.Path(options.env["CLAUDE_TEST_LOG"]).read_text()).splitlines()[0]
    )
    with pytest.raises(ProcessLookupError):
        os.kill(info.pid, 0)


@pytest.mark.parametrize("cancel_task", [False, True])
async def test_interrupt_and_cancellation_release_process_and_lease(
    manager: HabitatManager,
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
        async with (
            manager.enter(scope, uuid7()) as residence,
            HabitatTransport.client(residence.habitat, options) as client,
        ):
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
    assert not manager.residence(scope).invocations
    info = ProcessInfo.model_validate_json(
        (await anyio.Path(options.env["CLAUDE_TEST_LOG"]).read_text()).splitlines()[0]
    )
    with pytest.raises(ProcessLookupError):
        os.kill(info.pid, 0)


async def test_a_driven_turn_runs_inside_the_owners_sandbox(
    in_memory_engine: AsyncEngine,
    executable: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    log = tmp_path / "protocol.jsonl"
    cli = tmp_path / "claude"
    cli.write_text(
        f"#!{sys.executable}\n"
        f"import os\nos.environ['CLAUDE_TEST_LOG'] = {str(log)!r}\n"
        + Path(__file__).with_name("fixtures").joinpath("stdio_cli.py").read_text()
    )
    cli.chmod(0o700)
    execute = SbxHabitat.execute
    executions: list[tuple[str, ...]] = []

    @asynccontextmanager
    async def capture_execution(
        habitat: SbxHabitat,
        *command: str,
        workdir: str,
        env: dict[str, str] | None = None,
    ) -> AsyncGenerator[ByteStream]:
        assert env is not None
        assert env["OCTOMATE_CLI_URL"] == "http://host.docker.internal:8000"
        assert env["OCTOMATE_CLI_TOKEN"]
        executions.append(command)
        async with execute(habitat, *command, workdir=workdir, env=env) as stream:
            yield stream

    monkeypatch.setattr(SbxHabitat, "execute", capture_execution)
    # The guest finds `claude` on its own path; the stand-in sbx shares the host's.
    monkeypatch.setenv("PATH", f"{tmp_path}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setattr(SbxHabitat, "prepare", AsyncMock())
    monkeypatch.setattr(SbxHabitat, "stop", AsyncMock())
    host = Octomate(
        config=OctomateConfig(auth=auth_config()),
        conversations=FakeConversationManager(),
    )
    host.habitats.config = SbxHabitatConfig(executable=str(executable), image=IMAGE)
    tentacle = ClaudeTentacle(
        host,
        auth=host.auth,
        habitats=host.habitats,
        config=ClaudeCodeConfig(),
        commands=host.commands,
        projects=host.projects,
        threads=host.threads,
        files=host.files,
        conversations=host.conversations,
        deferred_actions=host.deferred_actions,
        workspaces=host.workspaces,
        users=host.users,
        bearers=host.bearers,
        mcp=host.mcp,
    )
    owner = await a_user()
    address = ChannelAddress(
        channel_tentacle_id="im", chat_type="dm", chat_id="alice", user_id="alice"
    )
    results: list[AgentRunResultEvent] = []
    try:
        async with (
            asyncio.timeout(10),
            invocation_for(
                tentacle,
                conversation_address=address,
                thread_id=await a_thread(),
                owner_id=owner.id,
            ) as invocation,
            # The scripted CLI asks to use a tool; with no user to ask, the
            # run declines over the same connection and the turn finishes.
            tentacle.run_stream_events(
                "Run the tool",
                conversation_address=address,
                invocation=invocation,
                run_name="react",
                interactive=False,
            ) as stream,
        ):
            results = [
                event
                async for event in stream
                if isinstance(event, AgentRunResultEvent)
            ]
            assert isinstance(invocation.habitat, SbxHabitat)
            assert host.habitats.residences[owner.id].invocations == {invocation.id}
    finally:
        await tentacle.revoke_runtime_keys()
        await host.habitats.close()
    assert [event.result.output for event in results] == ["Approved"]
    stored = await tentacle.conversations.get(invocation.conversation.id)
    assert stored.external_id == "scripted-session"
    assert len(executions) == 1
    assert executions[0][0] == "claude"
    assert not host.habitats.residences[owner.id].invocations
    info = ProcessInfo.model_validate_json(
        (await anyio.Path(log).read_text()).splitlines()[0]
    )
    assert info.cwd == str(invocation.cwd)
    assert info.config == invocation.environment["CLAUDE_CONFIG_DIR"]
    assert "--safe-mode" not in info.argv
    # The sandbox is the boundary: Claude's own is not configured, and no
    # host settings are loaded.
    settings = json.loads(info.argv[info.argv.index("--settings") + 1])
    assert settings == {
        "hooks": {
            event: [
                {
                    "hooks": [
                        {
                            "type": "command",
                            "command": "octomate claude upload "
                            f"--conversation {invocation.conversation.id}",
                            "timeout": 60,
                        }
                    ]
                }
            ]
            for event in ("Stop", "SessionEnd")
        }
    }
    assert "--setting-sources=" in info.argv
    with pytest.raises(ProcessLookupError):
        os.kill(info.pid, 0)
