"""Real guest Codex turns with a scripted model and persistent habitat client."""

import json
import os
from contextlib import AsyncExitStack
from pathlib import Path

import pytest
from httpx2 import URL
from openai_codex import CodexConfig as RuntimeConfig
from pydantic import HttpUrl
from sqlalchemy.ext.asyncio import AsyncEngine
from uuid_utils.compat import uuid7

from octomate import Octomate
from octomate.config import OctomateConfig
from octomate.config.agents import CodexConfig
from octomate.config.habitat import SbxHabitatConfig
from octomate.habitat.sbx import SbxHabitat
from octomate.schemas.conversation import ChannelAddress
from octomate.schemas.habitat import InvocationScope
from octomate.schemas.thread import ThreadKey
from octomate.tentacles.codex import CodexTentacle
from tests.agent.claude.test_driven_transcript import served
from tests.integration.native_sandbox.provider import ScriptedProvider, local_provider
from tests.integration.sbx import cli_pids, remove_sandbox
from tests.support.invocations import invocation_for
from tests.support.managers import a_project, a_registry
from tests.support.users import a_user, auth_config


@pytest.mark.timeout(600)
async def test_driven_turns_reuse_the_guest_client_and_native_session(
    in_memory_engine: AsyncEngine,
    sbx_cleanup: AsyncExitStack,
    tmp_path: Path,
) -> None:
    image = os.environ.get("SBX_TEST_IMAGE")
    if not image:
        pytest.fail("Set SBX_TEST_IMAGE to the immutable SDK image reference")
    core_home = tmp_path / "core-codex"
    core_home.mkdir()
    state = ScriptedProvider("responses")
    host = Octomate(config=OctomateConfig(auth=auth_config()))
    host.habitats.config = SbxHabitatConfig(image=image)
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
    sandbox = host.habitats.residence(
        InvocationScope(user_id=owner.id, conversation_id=uuid7())
    ).habitat
    assert isinstance(sandbox, SbxHabitat)
    sbx_cleanup.push_async_callback(remove_sandbox, sandbox)
    sbx_cleanup.push_async_callback(host.habitats.close)

    async with AsyncExitStack() as resources:
        provider_url = resources.enter_context(local_provider(state))
        guest_url = str(URL(provider_url).copy_with(host="host.docker.internal"))
        runtime = RuntimeConfig(
            env={"CODEX_HOME": str(core_home), "OPENAI_API_KEY": ""},
            config_overrides=(
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
            ),
        )
        tentacle = CodexTentacle(
            host,
            habitats=host.habitats,
            config=CodexConfig(runtime=runtime, permission_mode="full_access"),
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
        await resources.enter_async_context(host.fastmcp.lifespan(host.fastmcp))
        base = await resources.enter_async_context(served(host))
        await resources.enter_async_context(tentacle)
        sandbox.config = sandbox.config.model_copy(
            update={"core_address": HttpUrl(base)}
        )
        previous = None
        pids = []
        external_id = None
        for command in (
            "printf habitat-proof > proof.txt; uname -s",
            "cat proof.txt; uname -s",
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
                result = await tentacle.run(
                    "Execute the synthetic probe once.",
                    conversation_address=address,
                    invocation=invocation,
                    interactive=False,
                )
                assert result.output == "done"
                assert state.outcome is not None
                assert "Linux" in state.outcome.text
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
                assert previous is None or client is previous
                assert not pids or current_pids == pids
                assert external_id is None or conversation.external_id == external_id
                assert previous is None or "habitat-proof" in state.outcome.text
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
            assert not host.habitats.residence(
                InvocationScope(
                    user_id=owner.id, conversation_id=invocation.conversation.id
                )
            ).invocations
        assert not list(core_home.glob("sessions/**/*.jsonl"))
