"""Real Codex tools in workspace-scoped SRT, with a retained host agent loop."""

import asyncio
import json
import os
from collections.abc import AsyncIterator
from dataclasses import replace
from pathlib import Path
from typing import Literal
from unittest.mock import AsyncMock, Mock

import pytest
from httpx2 import URL
from openai_codex import AsyncThread
from openai_codex import CodexConfig as RuntimeConfig
from openai_codex.api import Sandbox
from openai_codex.generated.v2_all import (
    ThreadUnsubscribeResponse,
    TurnEnvironmentParams,
)
from uuid_utils.compat import uuid7

from octomate.config.agents import CodexConfig
from octomate.habitat.srt import SrtHabitat
from octomate.managers.habitat import Invocation
from octomate.schemas.conversation import Conversation
from octomate.schemas.volumes import Volume
from octomate.schemas.volumes.storage import HostDirectory
from octomate.schemas.workspaces import Workspace
from octomate.tentacles.codex.ink import CodexInk
from tests.integration.native_sandbox.conftest import sandbox_tree as sandbox_tree
from tests.integration.native_sandbox.matrix import (
    SERVER_CANARY,
    SHELL_CHECKS,
    NativeCall,
    SandboxTree,
    ToolOutcome,
)
from tests.integration.native_sandbox.provider import ScriptedProvider, local_provider
from tests.integration.native_sandbox.test_boundaries import (
    test_server_environment_is_hidden as test_server_environment_is_hidden,
)
from tests.integration.native_sandbox.test_boundaries import (
    test_shell_boundary as check_shell_boundary,
)

pytestmark = pytest.mark.timeout(60)


@pytest.fixture
async def scenario(
    habitat: SrtHabitat,
    sandbox_tree: SandboxTree,
) -> AsyncIterator[tuple[CodexInk, Invocation, ScriptedProvider]]:
    await habitat.stop()
    habitat.root = sandbox_tree.workspace.parent
    await habitat.prepare()
    workspace = sandbox_tree.workspace
    home = habitat.root / "core-home"
    home.mkdir()
    thread_id = uuid7()
    invocation = Invocation(
        id=uuid7(),
        habitat=habitat,
        workspace=Workspace(
            thread_id=thread_id,
            volumes={
                "work": Volume(
                    owner_id=habitat.user_id, storage=HostDirectory(workspace)
                ),
                "config": Volume(
                    owner_id=habitat.user_id,
                    storage=HostDirectory(habitat.root / "config"),
                ),
            },
        ),
        conversation=Conversation(thread_id=thread_id, agent_tentacle_id="codex"),
    )
    state = ScriptedProvider("responses", tool="exec_command")
    with local_provider(state) as url:
        runtime = RuntimeConfig(
            cwd=str(home),
            env={"CODEX_HOME": str(home), "OPENAI_API_KEY": ""},
            config_overrides=(
                'model_provider="audit"',
                "model_providers.audit="
                + json.dumps(
                    {
                        "name": "audit",
                        "base_url": url + "/v1",
                        "wire_api": "responses",
                        "requires_openai_auth": False,
                    }
                ).replace(": ", " = "),
            ),
        )
        ink = CodexInk(
            CodexConfig(runtime=runtime),
            agent_id="codex",
            mcp_url=URL("http://127.0.0.1/mcp"),
            handler=AsyncMock(
                side_effect=AssertionError("Unexpected approval request")
            ),
        )
        await ink.start(invalidate_commands=Mock())
        try:
            yield ink, invocation, state
        finally:
            await ink.close()


async def run_probe(
    ink: CodexInk,
    invocation: Invocation,
    state: ScriptedProvider,
    thread: AsyncThread,
    environment: TurnEnvironmentParams | None,
    command: str | None,
    sandbox: Sandbox = Sandbox.workspace_write,
) -> ToolOutcome:
    state.call_id = str(uuid7())
    state.outcome = None
    if command is not None:
        state.arguments = {"cmd": command, "max_output_tokens": 1000}
    turn = await ink.start_turn(
        thread,
        "Execute the synthetic probe once.",
        approval_mode=None,
        sandbox=sandbox,
        cwd=str(invocation.cwd),
        effort=None,
        model=None,
        output_schema=None,
        personality=None,
        summary=None,
        environment=environment,
    )
    try:
        await asyncio.wait_for(turn.run(), 15)
    except TimeoutError:
        await turn.interrupt()
        raise
    assert state.outcome is not None
    return state.outcome


async def test_host_codex_reuses_thread_with_fresh_executors_and_fork(
    scenario: tuple[CodexInk, Invocation, ScriptedProvider],
) -> None:
    ink, invocation, state = scenario
    thread_id = None
    executor_pids = []
    client = await ink.pool.acquire(invocation.habitat, env=invocation.environment)
    assert client.transport.stream is None
    process = client.transport._proc
    assert process is not None
    peer = Path(invocation.cwd).parent / "canary"
    peer.write_text("synthetic-private")
    for turn_number in range(4):
        if turn_number == 3:
            fork_id = await ink.fork_session(
                thread_id,
                cwd=invocation.cwd,
                habitat=invocation.habitat,
                env=invocation.environment,
            )
            assert fork_id != thread_id
            thread_id = fork_id
        async with ink.workspace(invocation, Sandbox.workspace_write) as environment:
            if turn_number == 1:
                assert thread_id is not None
                await ink.resume_thread(
                    client=client,
                    thread_id=thread_id,
                    approval_mode=None,
                    base_instructions=None,
                    config={},
                    cwd=str(invocation.cwd),
                    developer_instructions=None,
                    model="gpt-6-luna",
                    model_provider="audit",
                    personality=None,
                    sandbox=Sandbox.workspace_write,
                    environment=environment,
                )
            thread, _ = await ink.open_thread(
                habitat=invocation.habitat,
                env=invocation.environment,
                thread_id=thread_id,
                conversation_id=invocation.conversation.id,
                api_key_id=None,
                mcp_bearer=None,
                cwd=str(invocation.cwd),
                approval_mode=None,
                base_instructions=None,
                developer_instructions=None,
                ephemeral=False,
                model="gpt-6-luna",
                model_provider="audit",
                personality=None,
                sandbox=Sandbox.workspace_write,
                environment=environment,
            )
            if thread_id is not None:
                assert thread.id == thread_id
            thread_id = thread.id
            outcome = await run_probe(
                ink,
                invocation,
                state,
                thread,
                environment,
                "printf inside > proof; cat proof" if turn_number == 0 else "cat proof",
            )
            assert "inside" in outcome.text
            outcome = await run_probe(
                ink,
                invocation,
                state,
                thread,
                environment,
                f"echo EXECUTOR=$PPID; if cat {peer}; then printf OUTSIDE_ALLOWED; else printf OUTSIDE_DENIED; fi",
            )
            assert "OUTSIDE_DENIED" in outcome.text
            assert "OUTSIDE_ALLOWED" not in outcome.text
            line = next(
                line
                for line in outcome.text.splitlines()
                if line.startswith("EXECUTOR=")
            )
            executor_pids.append(int(line.removeprefix("EXECUTOR=")))
            assert (
                await ink.pool.acquire(invocation.habitat, env=invocation.environment)
                is client
            )
            assert client.transport._proc is process
        with pytest.raises(ProcessLookupError):
            os.kill(executor_pids[-1], 0)
        if turn_number == 0:
            await client._client.request(
                "thread/unsubscribe",
                {"threadId": thread_id},
                response_model=ThreadUnsubscribeResponse,
            )
    assert len(set(executor_pids)) == 4
    assert isinstance(invocation.habitat, SrtHabitat)
    assert not invocation.habitat.processes
    assert not (Path(invocation.cwd) / "config").exists()
    assert not (Path(invocation.cwd) / "cache").exists()


@pytest.mark.parametrize("sandbox", [Sandbox.workspace_write, Sandbox.read_only])
async def test_workspace_policy_is_enforced_by_executor(
    scenario: tuple[CodexInk, Invocation, ScriptedProvider], sandbox: Sandbox
) -> None:
    ink, invocation, state = scenario
    async with ink.workspace(invocation, sandbox) as environment:
        client = await ink.pool.acquire(invocation.habitat, env=invocation.environment)
        thread, _ = await ink.start_thread(
            client=client,
            approval_mode=None,
            base_instructions=None,
            config={},
            cwd=str(invocation.cwd),
            developer_instructions=None,
            ephemeral=False,
            model="gpt-6-luna",
            model_provider="audit",
            personality=None,
            sandbox=sandbox,
            environment=environment,
        )
        outcome = await run_probe(
            ink,
            invocation,
            state,
            thread,
            environment,
            "printf probe > proof; echo WRITE_EXIT=$?",
            sandbox,
        )
        assert f"WRITE_EXIT={1 if sandbox == Sandbox.read_only else 0}" in outcome.text


@pytest.fixture
async def native_call(
    scenario: tuple[CodexInk, Invocation, ScriptedProvider],
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncIterator[NativeCall]:
    ink, invocation, state = scenario
    monkeypatch.setenv(SERVER_CANARY, "synthetic-server-only")
    async with ink.workspace(invocation, Sandbox.workspace_write) as environment:
        client = await ink.pool.acquire(invocation.habitat, env=invocation.environment)
        thread, _ = await ink.start_thread(
            client=client,
            approval_mode=None,
            base_instructions=None,
            config={},
            cwd=str(invocation.cwd),
            developer_instructions=None,
            ephemeral=False,
            model="gpt-6-luna",
            model_provider="audit",
            personality=None,
            sandbox=Sandbox.workspace_write,
            environment=environment,
        )

        async def call(kind: Literal["shell", "file"], command: str) -> ToolOutcome:
            assert kind == "shell"
            return await run_probe(ink, invocation, state, thread, environment, command)

        yield call


@pytest.mark.parametrize(
    "check",
    [
        pytest.param(
            check,
            marks=pytest.mark.xfail(
                strict=True,
                reason="Accepted SRT directory metadata access permits cd; file access stays confined",
            ),
        )
        if check == "outside_cwd"
        else check
        for check in SHELL_CHECKS
    ],
)
async def test_shell_boundary(
    check: str, sandbox_tree: SandboxTree, native_call: NativeCall
) -> None:
    await check_shell_boundary(check, sandbox_tree, native_call)


async def test_no_executor_does_not_fall_back_to_host(
    scenario: tuple[CodexInk, Invocation, ScriptedProvider],
) -> None:
    ink, invocation, state = scenario
    client = await ink.pool.acquire(invocation.habitat, env=invocation.environment)
    thread, _ = await ink.start_thread(
        client=client,
        approval_mode=None,
        base_instructions=None,
        config={},
        cwd=str(invocation.cwd),
        developer_instructions=None,
        ephemeral=False,
        model="gpt-6-luna",
        model_provider="audit",
        personality=None,
        sandbox=Sandbox.workspace_write,
    )
    marker = Path(invocation.cwd).parent / "host-fallback"
    outcome = await run_probe(
        ink,
        invocation,
        state,
        thread,
        None,
        f"printf HOST_LEAK > {marker}; cat {marker}",
    )
    assert not marker.exists()
    assert "HOST_LEAK" not in outcome.text


async def test_two_workspaces_share_host_without_sharing_execution(
    scenario: tuple[CodexInk, Invocation, ScriptedProvider],
) -> None:
    ink, invocation, state = scenario
    other_path = Path(invocation.cwd).parent / "other-workspace"
    other_path.mkdir()
    (other_path / "secret").write_text("peer-private")
    peer_thread = uuid7()
    other = replace(
        invocation,
        id=uuid7(),
        conversation=Conversation(thread_id=peer_thread, agent_tentacle_id="codex"),
        workspace=Workspace(
            thread_id=peer_thread,
            volumes={
                **invocation.workspace.volumes,
                "work": Volume(
                    owner_id=invocation.owner_id, storage=HostDirectory(other_path)
                ),
            },
        ),
    )
    async with (
        ink.workspace(invocation, Sandbox.workspace_write) as first,
        ink.workspace(other, Sandbox.workspace_write) as second,
    ):
        client = await ink.pool.acquire(invocation.habitat, env=invocation.environment)
        assert await ink.pool.acquire(other.habitat, env=other.environment) is client
        for selected, environment in ((invocation, first), (other, second)):
            thread, _ = await ink.start_thread(
                client=client,
                approval_mode=None,
                base_instructions=None,
                config={},
                cwd=str(selected.cwd),
                developer_instructions=None,
                ephemeral=False,
                model="gpt-6-luna",
                model_provider="audit",
                personality=None,
                sandbox=Sandbox.workspace_write,
                environment=environment,
            )
            outcome = await run_probe(
                ink,
                selected,
                state,
                thread,
                environment,
                f"cat {other_path}/secret; echo READ_EXIT=$?",
            )
            assert f"READ_EXIT={1 if selected is invocation else 0}" in outcome.text
            assert ("peer-private" in outcome.text) == (selected is other)


@pytest.mark.parametrize("target", ["workspace", "outside", "symlink", "read-only"])
async def test_native_patch_uses_workspace_executor(
    scenario: tuple[CodexInk, Invocation, ScriptedProvider],
    target: str,
) -> None:
    ink, invocation, state = scenario
    root = Path(invocation.cwd)
    path = (
        root.parent / "outside/patched.txt"
        if target == "outside"
        else root / "outside-link/patched.txt"
        if target == "symlink"
        else root / "patched.txt"
    )
    path.write_text("host-control")
    path.unlink()
    sandbox = Sandbox.read_only if target == "read-only" else Sandbox.workspace_write
    async with ink.workspace(invocation, sandbox) as environment:
        client = await ink.pool.acquire(invocation.habitat, env=invocation.environment)
        thread, _ = await ink.start_thread(
            client=client,
            approval_mode=None,
            base_instructions=None,
            config={},
            cwd=str(invocation.cwd),
            developer_instructions=None,
            ephemeral=False,
            model="gpt-6-luna",
            model_provider="audit",
            personality=None,
            sandbox=sandbox,
            environment=environment,
        )
        state.tool = "apply_patch"
        state.custom_input = (
            f"*** Begin Patch\n*** Add File: {path}\n+PATCH_PROOF\n*** End Patch"
        )
        outcome = await run_probe(
            ink, invocation, state, thread, environment, None, sandbox
        )
        assert "unsupported custom tool" not in outcome.text
        assert path.exists() == (target == "workspace"), outcome.text
        if target == "workspace":
            assert path.read_text() == "PATCH_PROOF\n"
