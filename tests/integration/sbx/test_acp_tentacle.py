"""Scripted ACP providers cross a real microVM's stdio and session boundary."""

import asyncio
import json
import os
from contextlib import AsyncExitStack
from pathlib import Path

import acp
import pytest
from acp import schema
from acp.utils import notify_model, request_model
from pydantic import HttpUrl, SecretStr
from pydantic_ai import AgentRunResultEvent
from pydantic_ai.messages import PartStartEvent
from sqlalchemy.ext.asyncio import AsyncEngine
from uuid_utils.compat import uuid7

from octomate import Octomate
from octomate.config import AcpAgentConfig, OctomateConfig
from octomate.config.acp import AcpProvider
from octomate.config.habitat import SbxHabitatConfig
from octomate.habitat.sbx import SbxHabitat
from octomate.managers.habitat import HabitatManager
from octomate.nerve import Nerve
from octomate.schemas.habitat import InvocationScope
from octomate.schemas.thread import ThreadKey
from octomate.tentacles.acp.base import AcpAgentTentacle
from octomate.tentacles.acp.client import NativeAcpClient
from tests.agent.acp.test_client import Callbacks
from tests.integration.sbx import cli_pids, remove_sandbox
from tests.support.invocations import invocation_for
from tests.support.managers import a_workspace_manager
from tests.support.users import a_user, auth_config


@pytest.fixture
def provider(tmp_path: Path) -> AcpProvider:
    source = (Path(__file__).parents[2] / "agent/acp/fixtures/acp_agent.py").read_text()
    core_home = tmp_path / "core-acp"
    core_home.mkdir()
    return AcpProvider(
        command="python",
        args=[
            "-c",
            "import os; from pathlib import Path; "
            "os.environ['ACP_TEST_LOG'] = str(Path.home() / 'acp-requests.jsonl')\n"
            + source,
            "octomate-acp-acceptance",
        ],
        env={
            "HOME": SecretStr(str(core_home)),
            "ACP_TEST_MODELS": SecretStr("grouped"),
            "ACP_TEST_TEXT": SecretStr("guest ACP answer"),
        },
    )


@pytest.mark.timeout(600)
async def test_turns_stream_resume_and_use_guest_paths(
    provider: AcpProvider,
    in_memory_engine: AsyncEngine,
    sbx_cleanup: AsyncExitStack,
) -> None:
    image = os.environ.get("SBX_TEST_IMAGE")
    if not image:
        pytest.fail("Set SBX_TEST_IMAGE to the immutable SDK image reference")
    host = Octomate(config=OctomateConfig(auth=auth_config()))
    host.habitats.config = SbxHabitatConfig(
        image=image, core_address=HttpUrl("https://core.example:9443")
    )
    agent = host.connect(
        AcpAgentTentacle(
            "custom",
            host,
            auth=host.auth,
            habitats=host.habitats,
            config=AcpAgentConfig(provider=provider),
        )
    )
    owner = await a_user("acp-owner")
    sender = await host.users.native_profile(agent.id, owner.username)
    assert sender is not None
    sender = await host.users.ensure_profile(agent.id, sender)
    thread = await host.threads.ensure(
        ThreadKey(agent.id, "thread", str(owner.id), uuid7().hex)
    )
    sandbox = host.habitats.residence(
        InvocationScope(user_id=owner.id, conversation_id=uuid7())
    ).habitat
    assert isinstance(sandbox, SbxHabitat)
    sbx_cleanup.push_async_callback(remove_sandbox, sandbox)
    sbx_cleanup.push_async_callback(host.habitats.close)
    external_id = None
    await sbx_cleanup.enter_async_context(agent)
    assert set(agent.models) == {"fast", "careful"}
    for method in ("session/new", "session/load"):
        async with invocation_for(
            agent,
            conversation_address=thread.key.address(sender.channel_user_id),
            thread_id=thread.id,
            owner_id=owner.id,
        ) as invocation:
            async with agent.run_stream_events(
                "hello",
                invocation=invocation,
                conversation_address=thread.key.address(sender.channel_user_id),
                interactive=False,
            ) as stream:
                events = [event async for event in stream]
            assert any(isinstance(event, PartStartEvent) for event in events)
            assert isinstance(events[-1], AgentRunResultEvent)
            assert events[-1].result.output == "guest ACP answer"
            assert invocation.habitat is sandbox
            conversation = await host.conversations.get(
                invocation.conversation.id, with_runs=False
            )
            current_id = conversation.external_id
            assert current_id is not None
            if external_id is not None:
                assert current_id == external_id
            external_id = current_id
            config = invocation.workspace.volumes.get("config")
            assert config is not None
            home = sandbox.path(config)
            raw = await sandbox.command(
                "exec", sandbox.name, "cat", str(home / "acp-requests.jsonl")
            )
            records = [json.loads(line) for line in raw.splitlines()]
            assert records[0]["cwd"] == str(invocation.cwd)
            assert records[0]["home"] == str(home)
            assert records[0]["codex_home"] == str(home / ".codex")
            messages = [record["message"] for record in records]
            request = next(
                item["params"] for item in messages if item.get("method") == method
            )
            assert request["cwd"] == str(invocation.cwd)
            assert (
                request["mcpServers"][0]["url"]
                == "https://host.docker.internal:9443/octomate/mcp"
            )
            reply = next(item for item in messages if item.get("id") == "permission")
            assert reply["result"]["outcome"] == {"outcome": "cancelled"}
            assert (
                await sandbox.command("exec", sandbox.name, "uname", "-s") == b"Linux\n"
            )
            await sandbox.command(
                "exec",
                sandbox.name,
                "timeout",
                "5",
                "sh",
                "-c",
                'while test -d "/proc/$1"; do sleep 0.05; done',
                "sh",
                str(records[0]["pid"]),
            )
            assert not await cli_pids(
                sandbox, argument_pattern="octomate-acp-acceptanc[e]"
            )


@pytest.mark.timeout(600)
@pytest.mark.parametrize("cancel", [False, True])
async def test_permission_and_cancellation_cross_guest_stdio(
    provider: AcpProvider, sbx_cleanup: AsyncExitStack, cancel: bool
) -> None:
    image = os.environ.get("SBX_TEST_IMAGE")
    if not image:
        pytest.fail("Set SBX_TEST_IMAGE to the immutable SDK image reference")
    manager = HabitatManager(
        SbxHabitatConfig(image=image), Nerve(), a_workspace_manager()
    )
    scope = InvocationScope(user_id=uuid7(), conversation_id=uuid7())
    sandbox = manager.residence(scope).habitat
    assert isinstance(sandbox, SbxHabitat)
    sbx_cleanup.push_async_callback(remove_sandbox, sandbox)
    sbx_cleanup.push_async_callback(manager.close)
    callbacks = Callbacks()
    async with (
        manager.enter(scope, uuid7()),
        NativeAcpClient.connect(
            provider,
            callbacks,
            schema.InitializeRequest(protocol_version=1),
            habitat=sandbox,
            cwd="/home/agent/workspace",
            env={"HOME": "/home/agent"},
        ) as client,
    ):
        session = await client.new_session(
            schema.NewSessionRequest(cwd="/home/agent/workspace", mcp_servers=[])
        )
        async with asyncio.timeout(30), asyncio.TaskGroup() as tasks:
            prompt = tasks.create_task(
                request_model(
                    client.connection,
                    "session/prompt",
                    schema.PromptRequest(
                        session_id=session.session_id,
                        prompt=[acp.text_block("hello")],
                    ),
                    schema.PromptResponse,
                )
            )
            await callbacks.asked.wait()
            assert not prompt.done()
            assert callbacks.permissions[0].session_id == session.session_id
            assert callbacks.updates
            if cancel:
                await notify_model(
                    client.connection,
                    "session/cancel",
                    schema.CancelNotification(session_id=session.session_id),
                )
            else:
                callbacks.answered.set()
            assert (await prompt).stop_reason == ("cancelled" if cancel else "end_turn")
    raw = await sandbox.command(
        "exec", sandbox.name, "cat", "/home/agent/acp-requests.jsonl"
    )
    records = [json.loads(line) for line in raw.splitlines()]
    if not cancel:
        reply = next(
            record["message"]
            for record in records
            if record["message"].get("id") == "permission"
        )
        assert reply["result"]["outcome"] == {
            "outcome": "selected",
            "optionId": "yes",
        }
    await sandbox.command(
        "exec",
        sandbox.name,
        "timeout",
        "5",
        "sh",
        "-c",
        'while test -d "/proc/$1"; do sleep 0.05; done',
        "sh",
        str(records[0]["pid"]),
    )
    assert not await cli_pids(sandbox, argument_pattern="octomate-acp-acceptanc[e]")
