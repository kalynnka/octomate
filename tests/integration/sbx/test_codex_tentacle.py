"""Guest Codex reuse with a scripted provider or opt-in authenticated inference."""

import json
import os
from base64 import b64decode
from contextlib import AsyncExitStack
from io import BytesIO
from pathlib import Path

import pytest
from fastapi import UploadFile
from httpx2 import URL
from openai_codex import AsyncThread
from openai_codex import CodexConfig as RuntimeConfig
from openai_codex.api import ApprovalMode, Sandbox
from openai_codex.generated.v2_all import FsReadFileResponse, Thread
from pydantic import HttpUrl
from pydantic_ai.messages import ModelRequest, UserPromptPart
from sqlalchemy.ext.asyncio import AsyncEngine
from uuid_utils.compat import uuid7

from octomate import Octomate
from octomate.config import OctomateConfig
from octomate.config.agents import CodexConfig
from octomate.config.habitat import SbxHabitatConfig
from octomate.habitat.sbx import SbxHabitat
from octomate.managers.habitat import Invocation
from octomate.schemas.conversation import ChannelAddress
from octomate.schemas.habitat import InvocationScope
from octomate.schemas.thread import ThreadKey
from octomate.tentacles.codex import CodexTentacle
from tests.agent.claude.test_driven_transcript import served
from tests.integration.native_sandbox.provider import ScriptedProvider, local_provider
from tests.integration.sbx import cli_pids, expire_idle, remove_sandbox
from tests.support.invocations import invocation_for
from tests.support.managers import a_project, a_registry
from tests.support.users import a_user, auth_config


@pytest.mark.timeout(600)
@pytest.mark.parametrize(
    "live",
    [
        False,
        pytest.param(
            True,
            marks=pytest.mark.skipif(
                os.environ.get("SBX_TEST_CODEX_TURN") != "1",
                reason="Set SBX_TEST_CODEX_TURN=1 to use the sbx OpenAI credential",
            ),
        ),
    ],
    ids=["scripted", "authenticated"],
)
async def test_driven_turns_reuse_the_guest_client_and_native_session(
    in_memory_engine: AsyncEngine,
    sbx_cleanup: AsyncExitStack,
    tmp_path: Path,
    live: bool,
) -> None:
    image = os.environ.get("SBX_TEST_IMAGE")
    if not image:
        pytest.fail("Set SBX_TEST_IMAGE to the immutable SDK image reference")
    core_home = tmp_path / "core-codex"
    core_home.mkdir()
    state = ScriptedProvider("responses")
    host = Octomate(config=OctomateConfig(auth=auth_config()))
    host.habitats["native"].config = SbxHabitatConfig(image=image)
    owner = await a_user("codex-owner")
    project_root = tmp_path / "project"
    project_root.mkdir()
    project = a_project(project_root, name="codex-habitat")
    host.workspaces.projects = await a_registry(project)
    thread = await host.threads.ensure(
        ThreadKey("im", "thread", "codex-owner", "codex-work"), project=project
    )
    thread_id = thread.id
    address = ChannelAddress(
        "im",
        "thread",
        "codex-owner",
        user_id="codex-owner",
        channel_thread_id="codex-work",
    )
    sandbox = (
        host.habitats["native"]
        .residence(InvocationScope(user_id=owner.id, conversation_id=uuid7()))
        .habitat
    )
    assert isinstance(sandbox, SbxHabitat)
    sbx_cleanup.push_async_callback(remove_sandbox, sandbox)
    sbx_cleanup.push_async_callback(host.habitats["native"].close)

    async with AsyncExitStack() as resources:
        overrides: tuple[str, ...] = ()
        if not live:
            provider_url = resources.enter_context(local_provider(state))
            guest_url = str(URL(provider_url).copy_with(host="host.docker.internal"))
            overrides = (
                'model_provider="audit"',
                'model="gpt-5"',
                "model_providers.audit="
                + json.dumps(
                    {
                        "name": "audit",
                        "base_url": guest_url + "/v1",
                        "wire_api": "responses",
                        "requires_openai_auth": False,
                    }
                ).replace(": ", " = "),
            )
        runtime = RuntimeConfig(
            env={"CODEX_HOME": str(core_home)},
            config_overrides=overrides,
        )
        tentacle = CodexTentacle(
            host,
            habitats=host.habitats["native"],
            config=CodexConfig(runtime=runtime, permission_mode="user_review"),
            commands=host.commands,
            projects=host.projects,
            threads=host.threads,
            files=host.files,
            conversations=host.conversations,
            deferred_actions=host.deferred_actions,
            workspaces=host.workspaces,
            users=host.users,
            bearers=host.bearers,
            auth=host.auth,
            gateway_manager=host.gateway,
        )
        host.connect(tentacle)
        await resources.enter_async_context(host.lifespan(host))
        base = await resources.enter_async_context(served(host))
        await resources.enter_async_context(tentacle)
        sandbox.config = sandbox.config.model_copy(
            update={"core_address": HttpUrl(base)}
        )
        previous = None
        pids = []
        external_id = None
        memory_token = uuid7().hex
        boot = None
        for turn, command in enumerate(
            (
                "printf habitat-proof > proof.txt; uname -s",
                "cat proof.txt; uname -s",
                "cat proof.txt; uname -s",
            )
        ):
            state.tool = "exec_command"
            state.arguments = {"cmd": command, "max_output_tokens": 1000}
            state.call_id = str(uuid7())
            state.outcome = None
            async with invocation_for(
                tentacle,
                conversation_address=address,
                thread_id=thread_id,
                owner_id=owner.id,
            ) as invocation:
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
                prompt = "Execute the synthetic probe once."
                if live:
                    prompt = (
                        f"Remember this token for the next turn: {memory_token}. "
                        if previous is None
                        else "Recall the token I gave you in the previous turn. "
                    ) + (
                        f"Run exactly this shell command: {command!r}. "
                        "Reply with the remembered token and the command output. "
                        "Do not write the token to any file."
                    )
                result = await tentacle.run(
                    prompt,
                    conversation_address=address,
                    invocation=invocation,
                    interactive=False,
                    model=os.environ.get("SBX_TEST_CODEX_MODEL", "gpt-6.2-sol")
                    if live
                    else None,
                )
                if live:
                    assert memory_token in result.output
                    assert "Linux" in result.output
                    assert previous is None or "habitat-proof" in result.output
                    assert json.loads(
                        await sandbox.control(
                            "exec",
                            sandbox.id,
                            "cat",
                            f"{invocation.environment['CODEX_HOME']}/auth.json",
                        )
                    ) == {"OPENAI_API_KEY": "proxy-managed"}
                else:
                    assert result.output == "done"
                    assert state.outcome is not None
                    assert "Linux" in state.outcome.text
                    assert previous is None or "habitat-proof" in state.outcome.text
                ink = tentacle.ink
                client = await ink.pool.acquire(invocation.habitat)
                assert client is not await ink.pool.acquire()
                assert client.transport.config.env is not None
                assert (
                    client.transport.config.env["CODEX_HOME"]
                    == invocation.environment["CODEX_HOME"]
                )
                current_pids = await cli_pids(sandbox, argument_pattern="app-serve[r]")
                assert len(current_pids) == 1
                conversation = await host.conversations.get(
                    invocation.conversation.id, with_runs=False
                )
                assert conversation.external_id is not None
                expected_provider = "audit"
                if live:
                    mode = await sandbox.control(
                        "exec", sandbox.id, "printenv", "SBX_CRED_OPENAI_MODE"
                    )
                    expected_provider = (
                        "sandboxd" if mode.strip() == b"oauth" else "openai"
                    )
                native_thread = await AsyncThread(
                    client, conversation.external_id
                ).read()
                assert native_thread.thread.model_provider == expected_provider
                if boot is None:
                    assert previous is None or client is previous
                    assert not pids or current_pids == pids
                else:
                    assert client is not previous
                    assert previous is not None
                    assert previous.transport.closing
                assert external_id is None or conversation.external_id == external_id
                previous, pids, external_id = (
                    client,
                    current_pids,
                    conversation.external_id,
                )
                assert (
                    invocation.workspace.path / "proof.txt"
                ).read_text() == "habitat-proof"
                assert "octomate_driven" in await ink.mcp_status(
                    conversation.external_id
                )
            assert (
                not host.habitats["native"]
                .residence(
                    InvocationScope(
                        user_id=owner.id, conversation_id=invocation.conversation.id
                    )
                )
                .invocations
            )
            if turn == 1:
                boot = await expire_idle(
                    host.habitats["native"],
                    InvocationScope(
                        user_id=owner.id, conversation_id=invocation.conversation.id
                    ),
                )
        assert not list(core_home.glob("sessions/**/*.jsonl"))
        if not live:
            assert external_id is not None
            source = await AsyncThread(client, external_id).read(include_turns=True)
            assert len(source.thread.turns) == 3
            destination = str(invocation.cwd / "fork")
            await sandbox.control("exec", sandbox.id, "mkdir", "-p", destination)
            forked_id = await ink.fork_session(
                external_id,
                cwd=Path(destination),
                habitat=sandbox,
                env=invocation.environment,
                last_turn_id=source.thread.turns[0].id,
            )
            forked = AsyncThread(client, forked_id)
            history = await forked.read(include_turns=True)
            assert forked_id != external_id
            assert [turn.id for turn in history.thread.turns] == [
                source.thread.turns[0].id
            ]
            assert history.thread.cwd.root == destination
            state.call_id = str(uuid7())
            state.arguments = {"cmd": "pwd", "max_output_tokens": 1000}
            state.outcome = None
            continued = await forked.run(
                "Execute the synthetic probe once.",
                cwd=destination,
                approval_mode=ApprovalMode.deny_all,
                sandbox=Sandbox.full_access,
            )
            assert continued.final_response == "done"
            assert state.outcome is not None
            assert destination in state.outcome.text
            unchanged = await AsyncThread(client, external_id).read(include_turns=True)
            assert unchanged.thread.turns == source.thread.turns
            assert len((await forked.read(include_turns=True)).thread.turns) == 2
            assert await cli_pids(sandbox, argument_pattern="app-serve[r]") == pids
            await exercise_uploaded_history(tentacle, invocation, source.thread, state)
            assert await cli_pids(sandbox, argument_pattern="app-serve[r]") == pids
            assert not list(core_home.glob("sessions/**/*.jsonl"))
    assert previous is not None
    assert previous.transport.closing
    assert not await cli_pids(sandbox, argument_pattern="app-serve[r]")


async def exercise_uploaded_history(
    tentacle: CodexTentacle,
    invocation: Invocation,
    source: Thread,
    state: ScriptedProvider,
) -> None:
    """Import an upload whose native session ID does not exist in the guest."""
    owner_id = invocation.owner_id
    assert owner_id is not None
    habitat = invocation.habitat
    assert isinstance(habitat, SbxHabitat)
    client = await tentacle.ink.pool.acquire(thread_id=source.id)
    assert source.path is not None
    response = await client._client.request(
        "fs/readFile", {"path": source.path}, response_model=FsReadFileResponse
    )
    opening, remainder = b64decode(response.data_base64).split(b"\n", 1)
    native_id = str(uuid7())
    metadata = json.loads(opening)
    metadata["payload"]["id"] = native_id
    metadata["payload"]["session_id"] = native_id
    uploaded = json.dumps(metadata).encode() + b"\n" + remainder
    completed_id = source.turns[0].id
    end = 0
    for line in uploaded.splitlines(keepends=True):
        end += len(line)
        record = json.loads(line)
        if (
            record["type"] == "event_msg"
            and record["payload"].get("type") == "task_complete"
            and record["payload"].get("turn_id") == completed_id
        ):
            break
    else:
        pytest.fail("Native rollout has no completed first turn")
    native_thread = await tentacle.threads.ensure(
        ThreadKey("codex", "thread", str(owner_id), native_id), native=True
    )
    native = await tentacle.conversations.ensure(
        native_thread.id, agent_tentacle_id=tentacle.id
    )
    await tentacle.conversations.record_external_run(
        native,
        completed_id,
        [ModelRequest(parts=[UserPromptPart("Execute the synthetic probe once.")])],
        native_session_id=native_id,
        model_name="gpt-5",
        permission_mode="full_access",
        end_offset=end,
    )
    await tentacle.conversations.store_transcript(
        UploadFile(BytesIO(uploaded), filename="rollout.jsonl"),
        0,
        conversation=native,
        files=tentacle.files,
        owner_id=owner_id,
    )
    native = await tentacle.conversations.get(native.id)
    address = ChannelAddress(
        "im",
        "thread",
        "codex-owner",
        user_id="codex-owner",
        channel_thread_id="imported",
    )
    target_thread = await tentacle.threads.ensure(
        ThreadKey("im", "thread", "codex-owner", "imported"),
        project=invocation.project,
    )
    target = await tentacle.conversations.ensure(
        target_thread.id, agent_tentacle_id=tentacle.id
    )
    imported = await tentacle.fork_transcript(
        native, target, owner_id=owner_id, cwd=invocation.workspace.path
    )
    assert imported.external_id not in {None, native_id, source.id}
    assert imported.external_id is not None
    history = await AsyncThread(client, imported.external_id).read(include_turns=True)
    assert [turn.id for turn in history.thread.turns] == [completed_id]
    assert history.thread.turns[0].items == source.turns[0].items
    assert imported.transcript_file_id is not None
    assert (
        await tentacle.files.read(imported.transcript_file_id, owner_id=owner_id)
        == uploaded[:end]
    )
    assert native.transcript_file_id is not None
    assert (
        await tentacle.files.read(native.transcript_file_id, owner_id=owner_id)
        == uploaded
    )
    assert target.external_id is None
    state.call_id = str(uuid7())
    state.arguments = {"cmd": "pwd", "max_output_tokens": 1000}
    state.outcome = None
    async with invocation_for(
        tentacle,
        conversation_address=address,
        thread_id=target_thread.id,
        owner_id=owner_id,
    ) as destination:
        result = await tentacle.run(
            "Execute the synthetic probe once.",
            conversation_address=address,
            invocation=destination,
            interactive=False,
        )
        assert result.output == "done"
        assert state.outcome is not None
        assert str(destination.cwd) in state.outcome.text
    resumed = await AsyncThread(client, imported.external_id).read(include_turns=True)
    assert len(resumed.thread.turns) == 2
    assert resumed.thread.turns[0].items == source.turns[0].items
    # A failed import must not leave a private rollout behind in the guest.
    home = invocation.environment["CODEX_HOME"]
    imports = f"{home}/sessions/users/{owner_id.hex}"
    before = await habitat.control("exec", habitat.id, "ls", imports)
    with pytest.raises(RuntimeError, match="abort import"):
        async with tentacle.ink.import_transcript(
            uploaded,
            external_id=native_id,
            owner_id=owner_id,
            habitat=habitat,
            env=invocation.environment,
        ):
            raise RuntimeError("abort import")
    assert await habitat.control("exec", habitat.id, "ls", imports) == before
