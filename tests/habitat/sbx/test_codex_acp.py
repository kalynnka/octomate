"""The real Codex ACP adapter executes tools and reopens its guest session."""

import asyncio
import json
import os
from contextlib import AsyncExitStack
from pathlib import Path

import pytest
from httpx2 import URL
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import AsyncEngine
from uuid_utils.compat import uuid7

from octomate import Octomate
from octomate.config import AcpAgentConfig, OctomateConfig
from octomate.config.acp import AcpProvider
from octomate.config.habitat import SbxHabitatConfig
from octomate.habitat.sbx import SbxHabitat
from octomate.schemas.habitat import InvocationScope
from octomate.schemas.thread import ThreadKey
from octomate.tentacles.acp.base import AcpAgentTentacle
from tests.habitat.sbx import expire_idle, remove_sandbox
from tests.integration.native_sandbox.provider import ScriptedProvider, local_provider
from tests.support.invocations import invocation_for
from tests.support.managers import a_project, a_registry
from tests.support.users import a_user, auth_config


@pytest.mark.timeout(600)
async def test_codex_acp_tools_and_fresh_process_resume(
    in_memory_engine: AsyncEngine,
    tmp_path: Path,
    sbx_cleanup: AsyncExitStack,
) -> None:
    image = os.environ.get("SBX_TEST_IMAGE")
    executable = os.environ.get("CODEX_ACP_TEST_EXECUTABLE")
    if not image or not executable:
        pytest.fail(
            "Set SBX_TEST_IMAGE and CODEX_ACP_TEST_EXECUTABLE for live acceptance"
        )
    host = Octomate(config=OctomateConfig(auth=auth_config()))
    host.habitats.config = SbxHabitatConfig(image=image)
    owner = await a_user("codex-acp-owner")
    project_root = tmp_path / "project"
    project_root.mkdir()
    project = a_project(project_root, name="codex-acp-habitat")
    host.workspaces.projects = await a_registry(project)
    sandbox = host.habitats.residence(
        InvocationScope(user_id=owner.id, conversation_id=uuid7())
    ).habitat
    assert isinstance(sandbox, SbxHabitat)
    sbx_cleanup.push_async_callback(remove_sandbox, sandbox)
    sbx_cleanup.push_async_callback(host.habitats.close)
    sandbox.root.mkdir(parents=True, exist_ok=True)
    adapter = sandbox.root / "codex-acp.mjs"
    await asyncio.to_thread(
        adapter.write_bytes, await asyncio.to_thread(Path(executable).read_bytes)
    )
    core_home = tmp_path / "core-codex"
    core_home.mkdir()
    state = ScriptedProvider("responses")
    with local_provider(state) as endpoint:
        gateway = str(URL(endpoint).copy_with(host="host.docker.internal"))
        config = {
            "model_provider": "audit",
            "model": "gpt-5",
            "model_providers": {
                "audit": {
                    "name": "Scripted acceptance provider",
                    "base_url": gateway + "/v1",
                    "wire_api": "responses",
                    "requires_openai_auth": False,
                }
            },
        }
        agent = host.connect(
            AcpAgentTentacle(
                "codex-acp",
                host,
                auth=host.auth,
                habitats=host.habitats,
                config=AcpAgentConfig(
                    gateway=False,
                    provider=AcpProvider(
                        command="node",
                        args=[str(adapter)],
                        env={
                            "CODEX_PATH": SecretStr("codex"),
                            "CODEX_HOME": SecretStr(str(core_home)),
                            "CODEX_CONFIG": SecretStr(json.dumps(config)),
                            "MODEL_PROVIDER": SecretStr("audit"),
                            "NO_BROWSER": SecretStr("1"),
                            "INITIAL_AGENT_MODE": SecretStr("agent"),
                        },
                    ),
                ),
            )
        )
        sender = await host.users.native_profile(agent.id, owner.username)
        assert sender is not None
        sender = await host.users.ensure_profile(agent.id, sender)
        thread = await host.threads.ensure(
            ThreadKey(agent.id, "thread", str(owner.id), uuid7().hex), project=project
        )
        address = thread.key.address(sender.channel_user_id)
        async with agent:
            session_id = None
            boot = None
            for command in (
                "pwd; printf acp-proof > proof.txt && uname -s",
                "pwd; cat proof.txt && uname -s",
            ):
                state.tool = "exec_command"
                state.arguments = {"cmd": command, "max_output_tokens": 1000}
                state.call_id = str(uuid7())
                state.outcome = None
                async with invocation_for(
                    agent,
                    conversation_address=address,
                    thread_id=thread.id,
                    owner_id=owner.id,
                ) as invocation:
                    if boot is not None:
                        assert (
                            await sandbox.control(
                                "exec",
                                sandbox.name,
                                "cat",
                                "/proc/sys/kernel/random/boot_id",
                            )
                            != boot
                        )
                    await agent.run(
                        "Execute the scripted probe once",
                        invocation=invocation,
                        conversation_address=address,
                        interactive=False,
                    )
                    assert state.outcome is not None
                    assert "Linux" in state.outcome.text, state.outcome.text
                    assert (invocation.workspace.path / "proof.txt").is_file(), (
                        state.outcome.text
                    )
                    assert (
                        invocation.workspace.path / "proof.txt"
                    ).read_text() == "acp-proof"
                    conversation = await host.conversations.get(
                        invocation.conversation.id, with_runs=False
                    )
                    assert conversation.external_id is not None
                    if session_id is not None:
                        assert conversation.external_id == session_id
                        assert "acp-proof" in state.outcome.text
                    session_id = conversation.external_id
                if boot is None:
                    boot = await expire_idle(
                        host.habitats,
                        InvocationScope(
                            user_id=owner.id, conversation_id=invocation.conversation.id
                        ),
                    )
