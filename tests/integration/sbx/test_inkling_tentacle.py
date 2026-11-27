"""Inkling project tools, history and cancellation against a disposable microVM."""

import asyncio
import os
from collections.abc import AsyncIterator
from contextlib import AsyncExitStack
from pathlib import Path

import pytest
from pydantic_ai.messages import ModelMessage, ToolReturnPart
from pydantic_ai.models.function import (
    AgentInfo,
    DeltaToolCall,
    DeltaToolCalls,
    FunctionModel,
)
from pydantic_ai.tools import DeferredToolRequests, DeferredToolResults
from pydantic_ai.workspaces import WorkspaceTimeoutError
from sqlalchemy.ext.asyncio import AsyncEngine
from uuid_utils.compat import uuid7

from octomate import Octomate
from octomate.config.habitat import SbxHabitatConfig
from octomate.habitat.sbx import SbxHabitat
from octomate.schemas.habitat import InvocationScope
from octomate.schemas.thread import ThreadKey
from octomate.tentacles.inkling import InklingTentacle
from octomate.tentacles.inkling.agent import Agent
from octomate.tentacles.inkling.base import InklingOutput
from octomate.tentacles.inkling.workspace import HabitatWorkspaceBackend
from tests.integration.sbx import expire_idle, remove_sandbox
from tests.support.invocations import invocation_for
from tests.support.managers import a_project, a_registry
from tests.support.users import a_user


@pytest.mark.timeout(600)
async def test_project_tools_approve_resume_and_reuse_the_habitat(
    in_memory_engine: AsyncEngine,
    tmp_path: Path,
    sbx_cleanup: AsyncExitStack,
) -> None:
    image = os.environ.get("SBX_TEST_IMAGE")
    if not image:
        pytest.fail("Set SBX_TEST_IMAGE to the immutable SDK image reference")
    source = tmp_path / "project"
    source.mkdir()
    (source / "AGENTS.md").write_text("INKLING HABITAT ACCEPTANCE INSTRUCTIONS")
    host = Octomate()
    host.habitats["native"].config = SbxHabitatConfig(image=image)
    project = a_project(source)
    host.workspaces.projects = await a_registry(project)
    owner = await a_user("inkling-owner")
    thread = await host.threads.ensure(
        ThreadKey(
            channel_tentacle_id="im",
            chat_type="thread",
            chat_id="acceptance",
            channel_thread_id=uuid7().hex,
        ),
        project=project,
    )
    address = thread.key.address("inkling-owner")
    scope = InvocationScope(user_id=owner.id, conversation_id=uuid7())
    sandbox = host.habitats["native"].residence(scope).habitat
    assert isinstance(sandbox, SbxHabitat)
    sbx_cleanup.push_async_callback(remove_sandbox, sandbox)
    sbx_cleanup.push_async_callback(host.habitats["native"].close)
    requests = 0
    returned: list[ToolReturnPart] = []

    async def respond(
        messages: list[ModelMessage], info: AgentInfo
    ) -> AsyncIterator[str | DeltaToolCalls]:
        nonlocal requests
        assert "INKLING HABITAT ACCEPTANCE INSTRUCTIONS" in (info.instructions or "")
        returned[:] = [
            part
            for message in messages
            for part in message.parts
            if isinstance(part, ToolReturnPart)
        ]
        requests += 1
        match requests:
            case 1:
                yield {
                    0: DeltaToolCall(
                        name="write_file",
                        json_args='{"path":"proof.txt","content":"habitat-proof"}',
                        tool_call_id="write",
                    ),
                    1: DeltaToolCall(
                        name="run_command",
                        json_args='{"command":"uname -s; pwd"}',
                        tool_call_id="shell",
                    ),
                }
            case 3:
                assert any(part.tool_call_id == "shell" for part in returned)
                yield {
                    0: DeltaToolCall(
                        name="read_file",
                        json_args='{"path":"proof.txt"}',
                        tool_call_id="read",
                    )
                }
            case _:
                yield "done"

    agent: Agent[None, InklingOutput] = Agent(
        FunctionModel(stream_function=respond, model_name="scripted"),
        deps_type=type(None),
        output_type=[str, DeferredToolRequests],
    )
    tentacle = InklingTentacle(
        "inkling", host, auth=host.auth, habitats=host.habitats["native"], agent=agent
    )
    reference = None
    boot = None
    for turn in range(2):
        async with invocation_for(
            tentacle,
            conversation_address=address,
            thread_id=thread.id,
            owner_id=owner.id,
        ) as invocation:
            assert invocation.habitat is sandbox
            if boot is not None:
                assert (
                    await sandbox.control(
                        "exec",
                        sandbox.id,
                        "cat",
                        "/proc/sys/kernel/random/boot_id",
                    )
                    != boot
                )
            backend = HabitatWorkspaceBackend(invocation)
            if reference is not None:
                assert backend.ref == reference
            reference = backend.ref
            result = await tentacle.run(
                "write and inspect" if turn == 0 else "read again",
                conversation_address=address,
                invocation=invocation,
            )
            assert isinstance(result.output, DeferredToolRequests)
            assert {call.tool_name for call in result.output.approvals} == (
                {"write_file", "run_command"} if turn == 0 else {"read_file"}
            )
            if turn == 0:
                assert not await backend.exists(str(invocation.cwd / "proof.txt"))
            approvals = DeferredToolResults(
                approvals={call.tool_call_id: True for call in result.output.approvals}
            )
        # Re-enter through core, reloading persisted history as a new request does.
        async with invocation_for(
            tentacle,
            conversation_address=address,
            thread_id=thread.id,
            owner_id=owner.id,
        ) as invocation:
            result = await tentacle.run(
                conversation_address=address,
                invocation=invocation,
                deferred_tool_results=approvals,
            )
            assert result.output == "done"
            assert result.response.workspace_ref == reference
            saved = await host.conversations.get(invocation.conversation.id)
            assert saved is not None
            assert saved.runs
            assert all(run.cwd == Path(invocation.cwd) for run in saved.runs)
            shell = next(part for part in returned if part.tool_call_id == "shell")
            assert "Linux" in shell.model_response_str()
            assert str(invocation.cwd) in shell.model_response_str()
        if turn == 0:
            boot = await expire_idle(host.habitats["native"], scope)
    read = next(part for part in returned if part.tool_call_id == "read")
    assert "habitat-proof" in read.model_response_str()
    assert not (source / "proof.txt").exists()

    peer = InklingTentacle(
        "peer", host, auth=host.auth, habitats=host.habitats["native"], agent=agent
    )
    async with invocation_for(
        peer, conversation_address=address, thread_id=thread.id, owner_id=owner.id
    ) as invocation:
        assert invocation.habitat is sandbox
        backend = HabitatWorkspaceBackend(invocation)
        assert (
            await backend.read_bytes(str(invocation.cwd / "proof.txt"))
            == b"habitat-proof"
        )
        failed = await backend.run("printf useful-error >&2; exit 7", shell=True)
        assert failed.exit_code == 7
        assert failed.stderr == "useful-error"
        with pytest.raises(WorkspaceTimeoutError) as timed_out:
            await backend.run("printf partial >&2; sleep 30", shell=True, timeout=0.2)
        assert timed_out.value.stderr == "partial"
        sleeping = asyncio.create_task(
            backend.run("echo $$ > sleeping.pid; exec sleep 30", shell=True)
        )
        try:
            await sandbox.control(
                "exec",
                "--workdir",
                str(invocation.cwd),
                sandbox.id,
                "timeout",
                "15",
                "sh",
                "-c",
                "until test -s sleeping.pid; do sleep 0.05; done",
            )
        finally:
            sleeping.cancel()
            with pytest.raises(asyncio.CancelledError):
                await sleeping
        reaped = await backend.run(
            'kill -0 "$(cat sleeping.pid)" 2>/dev/null', shell=True
        )
        assert reaped.exit_code != 0
