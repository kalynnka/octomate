"""Real native turns stop through the nerve and resume their persisted workspace."""

import asyncio
import json
import os
import socket
from collections.abc import AsyncIterator
from contextlib import AsyncExitStack
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

import pytest
from anyio import Path as AsyncPath
from octomate_cli.streaming.deepseek import DshHistoryClient, new_entries
from openai_codex import CodexConfig as RuntimeConfig
from pydantic import SecretStr
from pydantic_ai.messages import ModelMessage, ModelRequest, ToolReturnPart
from pydantic_ai.models.function import (
    AgentInfo,
    DeltaToolCall,
    DeltaToolCalls,
    FunctionModel,
)
from sqlalchemy.ext.asyncio import AsyncEngine
from uuid_utils.compat import uuid7

from octomate import Octomate
from octomate.config import AcpAgentConfig, ChannelConfig, OctomateConfig
from octomate.config.acp import AcpProvider
from octomate.config.agents import CodexConfig, DeepseekConfig
from octomate.database import async_session
from octomate.managers.steps import StepStore
from octomate.nerve import AgentTopic
from octomate.schemas.commands import CommandError
from octomate.schemas.conversation import ChannelAddress
from octomate.schemas.events import MessageEvent
from octomate.schemas.segments import TextSegment
from octomate.schemas.thread import Kick, ThreadKey
from octomate.schemas.volumes import GitVolume
from octomate.signals import AsleepSignal, UserMessageSignal
from octomate.tentacles.acp.base import AcpAgentTentacle
from octomate.tentacles.codex import CodexTentacle
from octomate.tentacles.deepseek import DeepseekTentacle
from octomate.tentacles.inkling import InklingTentacle
from tests.agent.claude.test_driven_transcript import served
from tests.integration.native_sandbox.provider import ScriptedProvider, local_provider
from tests.support.channels import MainOnlyChannelTentacle
from tests.support.managers import a_project, a_registry
from tests.support.nerve import kick_and_wait
from tests.support.users import a_user, auth_config


@pytest.mark.parametrize("target", ["thread", "invocation"])
@pytest.mark.parametrize("harness", ["codex", "deepseek", "inkling", "acp"])
async def test_interrupt_then_resume_preserves_files_and_history(
    harness: Literal["codex", "deepseek", "inkling", "acp"],
    target: Literal["thread", "invocation"],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    in_memory_engine: AsyncEngine,
) -> None:
    host = Octomate(config=OctomateConfig(auth=auth_config()))
    owner = await a_user(profiles={"im": "owner"})
    address = ChannelAddress(
        "im", "thread", "interrupt", "owner", channel_thread_id="turn"
    )
    project_root = tmp_path / "project"
    project_root.mkdir()
    (project_root / "README.txt").write_text("Disposable interruption test workspace")
    project = a_project(project_root, name="interruption-test")
    host.workspaces.projects = await a_registry(project)
    thread = await host.threads.ensure(
        ThreadKey.model_validate(address), project=project
    )
    home = tmp_path / harness
    home.mkdir()
    first_command = "printf before > checkpoint.txt; sleep 30; printf late > late.txt"
    resume_command = "cat checkpoint.txt; printf resumed >> checkpoint.txt"
    state = ScriptedProvider(
        "responses",
        tool="exec_command",
        arguments={"cmd": first_command, "yield_time_ms": 1000},
        call_id=str(uuid7()),
    )
    async with AsyncExitStack() as resources:
        if harness == "codex":
            url = resources.enter_context(local_provider(state))
            runtime = RuntimeConfig(
                env={"CODEX_HOME": str(home), "HOME": str(home), "OPENAI_API_KEY": ""},
                config_overrides=(
                    'model_provider="audit"',
                    'model="gpt-5"',
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
            agent = CodexTentacle(
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
        elif harness == "acp":
            executable = os.environ.get("CODEX_ACP_TEST_EXECUTABLE")
            if executable is None:
                pytest.skip("set CODEX_ACP_TEST_EXECUTABLE to run the real ACP adapter")
            endpoint = resources.enter_context(local_provider(state))
            config = {
                "model_provider": "audit",
                "model": "gpt-5",
                "sandbox_mode": "danger-full-access",
                "approval_policy": "never",
                "model_providers": {
                    "audit": {
                        "name": "Scripted interruption provider",
                        "base_url": endpoint + "/v1",
                        "wire_api": "responses",
                        "requires_openai_auth": False,
                    }
                },
            }
            agent = AcpAgentTentacle(
                "codex-acp",
                host,
                auth=host.auth,
                habitats=host.habitats,
                config=AcpAgentConfig(
                    gateway=False,
                    provider=AcpProvider(
                        command=executable,
                        auth_method="api-key",
                        env={
                            "CODEX_HOME": SecretStr(str(home)),
                            "HOME": SecretStr(str(home)),
                            "CODEX_CONFIG": SecretStr(json.dumps(config)),
                            "MODEL_PROVIDER": SecretStr("audit"),
                            "NO_BROWSER": SecretStr("1"),
                            "INITIAL_AGENT_MODE": SecretStr("full-access"),
                            "OPENAI_API_KEY": SecretStr("synthetic-test-key"),
                        },
                    ),
                ),
            )
        elif harness == "deepseek":
            executable = os.environ.get("DSH_TEST_EXECUTABLE")
            if executable is None:
                pytest.skip("set DSH_TEST_EXECUTABLE to run the real DeepSeek harness")
            resolver = await asyncio.create_subprocess_exec(
                "node",
                "--input-type=module",
                "--eval",
                "console.log(import.meta.resolve('@deepseek-ai/dsh-llm'))",
                cwd=(await AsyncPath(executable).resolve()).parent,
                stdout=asyncio.subprocess.PIPE,
            )
            module, _ = await resolver.communicate()
            assert resolver.returncode == 0
            (home / "settings.yaml").write_text(
                "agent-default-model:\n  provider: octomate-test\n  model: mock\n"
            )
            command_path = tmp_path / "command.json"
            command_path.write_text(json.dumps(first_command))
            template = Path(__file__).with_name("deepseek_provider.mjs").read_text()
            plugin = tmp_path / "provider.mjs"
            plugin.write_text(
                template.replace(
                    "import { writeFileSync }", "import { readFileSync, writeFileSync }"
                )
                .replace("__DSH_LLM_MODULE__", module.decode().strip())
                .replace("const call = __AUDIT_CALL__;", "")
                .replace(
                    "async *stream(options) {",
                    "async *stream(options) {\n"
                    "const call = {name: 'bash', arguments: JSON.stringify({command: "
                    f"JSON.parse(readFileSync({json.dumps(str(command_path))}, 'utf8')), "
                    "description: 'Interruption test'})};",
                )
                .replace("__AUDIT_RESULT__", json.dumps(str(tmp_path / "result.json")))
            )
            patch = tmp_path / "patch.yml"
            patch.write_text(
                f"- insert:\n    - id: interrupt-test\n      name: {plugin}\n"
            )
            monkeypatch.setenv("DSH_TELEMETRY_DISABLED", "1")
            monkeypatch.setenv("DSH_AGENTS_HOME", str(tmp_path / "agents"))
            with socket.socket() as reservation:
                reservation.bind(("127.0.0.1", 0))
                port = reservation.getsockname()[1]
            agent = DeepseekTentacle(
                host,
                auth=host.auth,
                habitats=host.habitats,
                config=DeepseekConfig(
                    executable=executable,
                    port=port,
                    dsh_home=home,
                    extra_args=["--patch", str(patch)],
                ),
            )
        else:

            async def respond(
                messages: list[ModelMessage], info: AgentInfo
            ) -> AsyncIterator[str | DeltaToolCalls]:
                last = messages[-1]
                if isinstance(last, ModelRequest) and any(
                    isinstance(part, ToolReturnPart)
                    and part.tool_call_id == state.call_id
                    for part in last.parts
                ):
                    yield "done"
                    return
                yield {
                    0: DeltaToolCall(
                        name="run_command",
                        json_args=json.dumps({"command": state.arguments["cmd"]}),
                        tool_call_id=state.call_id,
                    )
                }

            agent = InklingTentacle(
                "inkling",
                host,
                auth=host.auth,
                habitats=host.habitats,
                models={"test": FunctionModel(stream_function=respond)},
                permission_mode="bypassPermissions",
            )
        host.connect(agent)
        host.connect(
            MainOnlyChannelTentacle(
                config=ChannelConfig(type="fake", agents=[agent.id])
            )
        )
        await resources.enter_async_context(host.fastmcp.lifespan(host.fastmcp))
        await resources.enter_async_context(host.mcp.lifespan())
        base = await resources.enter_async_context(served(host))
        port = urlsplit(base).port
        assert port is not None
        host.config.port = port
        await resources.enter_async_context(agent)
        if isinstance(agent, DeepseekTentacle):
            await host.threads.record_handoff(
                thread,
                to_agent_tentacle_id=agent.id,
                to_model="octomate-test:mock",
            )
        sender = await host.users.profile("im", "owner")
        assert sender is not None
        message = MessageEvent(
            tentacle_id="im",
            chat_type="thread",
            channel_thread_id="turn",
            chat_id="interrupt",
            user_id="owner",
            sender=sender,
            message_id=uuid7().hex,
            segments=[TextSegment(data={"text": "Execute the synthetic tool once."})],
        )
        inbound = await host.threads.record_inbound(message)
        prepared = await host.workspaces.resolve(thread.id, project, owner.id)
        workspace = prepared.path
        checkpoint = AsyncPath(workspace / "checkpoint.txt")
        task = asyncio.create_task(
            kick_and_wait(
                host,
                UserMessageSignal(
                    [message],
                    agent_id=None if harness == "deepseek" else agent.id,
                    trigger_thread_message_id=inbound.id,
                ),
            )
        )
        async with asyncio.timeout(60):
            # The filesystem is the native tool's readiness signal.
            while not await checkpoint.exists():
                if task.done():
                    pytest.fail(
                        f"run finished without writing its checkpoint: {await task}"
                    )
                await asyncio.sleep(0.02)
            host.nerves.signals.dispatch(
                AgentTopic(agent.id),
                AsleepSignal(thread=thread.key)
                if target == "thread"
                else AsleepSignal(invocation_id=next(iter(agent.invocations))),
            )
            interrupted = await task
        assert isinstance(interrupted, CommandError)
        assert interrupted.message == "Run interrupted."
        assert task.cancelling() == 0
        assert await checkpoint.read_text() == "before"
        assert not (workspace / "late.txt").exists()
        saved = await host.conversations.ensure(thread.id, agent_tentacle_id=agent.id)
        assert not agent.invocations
        # Inkling first defers approval, then executes the approved native tool.
        expected_runs = 2 if harness == "inkling" else 1
        assert len(saved.runs) == expected_runs
        assert saved.runs[0].messages
        if harness != "inkling":
            assert saved.external_id is not None
        first_message_ids = [
            message.id for run in saved.runs for message in run.messages
        ]
        assert isinstance(prepared.volume, GitVolume)
        assert await host.workspaces.saved(prepared.volume)
        assert await host.workspaces.reclaim(prepared.volume)
        assert not workspace.exists()
        if harness != "deepseek":
            state.arguments = {"cmd": resume_command, "yield_time_ms": 1000}
            state.call_id = str(uuid7())
            state.outcome = None
        else:
            (tmp_path / "command.json").write_text(json.dumps(resume_command))
        continued = message.model_copy(
            update={
                "message_id": uuid7().hex,
                "segments": [
                    TextSegment(data={"text": "Continue with the next synthetic tool."})
                ],
            }
        )
        inbound = await host.threads.record_inbound(continued)
        async with asyncio.timeout(60):
            result = await kick_and_wait(
                host,
                UserMessageSignal(
                    [continued],
                    agent_id=None if harness == "deepseek" else agent.id,
                    trigger_thread_message_id=inbound.id,
                ),
            )
        assert not isinstance(result, CommandError), result
        assert await checkpoint.read_text() == "beforeresumed"
        assert not (workspace / "late.txt").exists()
        resumed = await host.conversations.get(saved.id)
        assert resumed.external_id == saved.external_id
        assert len(resumed.runs) == expected_runs * 2
        assert [
            message.id
            for run in list(resumed.runs)[:expected_runs]
            for message in run.messages
        ] == first_message_ids
        assert all(run.messages for run in resumed.runs)
        if harness != "inkling":
            assert all(
                run.native_session_id == saved.external_id for run in resumed.runs
            )
        assert len({run.id for run in resumed.runs}) == expected_runs * 2
        assert not agent.invocations
        assert not agent.reflexes
        assert not host.awake_since
        async with async_session() as session:
            kicks = await session.list(
                Kick, expressions=[Kick["thread_id"] == thread.id]
            )
        assert [
            kick.outcome for kick in sorted(kicks, key=lambda kick: kick.woke_at)
        ] == [
            "cancelled",
            "completed",
        ]
        if harness in {"codex", "acp"}:
            transcripts = list(home.rglob("rollout-*.jsonl"))
            transcripts = [
                path
                for path in transcripts
                if saved.external_id and saved.external_id in path.name
            ]
            assert len(transcripts) == 1
            records = [
                json.loads(line) for line in transcripts[0].read_text().splitlines()
            ]
            transcript = json.dumps(records)
            assert saved.external_id is not None
            assert saved.external_id in transcript
            assert "checkpoint.txt" in transcript
            terminal = [
                record["payload"]["type"]
                for record in records
                if record.get("type") == "event_msg"
                and record["payload"].get("type") in {"turn_aborted", "task_complete"}
            ]
            assert terminal == ["turn_aborted", "task_complete"]
            assert (
                "beforeresumed" not in transcript
            )  # The second tool reads before appending.
            assert "Continue with the next synthetic tool." in transcript
        elif isinstance(agent, DeepseekTentacle):
            connection = await agent.ink.acquire()
            process = agent.ink.processes["core"]
            assert process.launch_token is not None
            assert saved.external_id is not None
            monkeypatch.setenv(
                "DSH_LAUNCH_TOKEN", process.launch_token.get_secret_value()
            )
            history = await asyncio.to_thread(
                DshHistoryClient, str(connection.client.base_url)
            )
            records = await asyncio.to_thread(
                new_entries, history, saved.external_id, 0
            )
            kinds = [
                event["type"]
                for entry in records
                if isinstance(event := entry.get("event"), dict)
            ]
            assert kinds.count("turn/end") == 2
            assert "tool/result" in kinds
            assert "checkpoint.txt" in json.dumps(records)
            result_data = json.loads((tmp_path / "result.json").read_text())
            assert "before" in result_data["text"]
        else:
            store = StepStore()
            first_snapshot = await store.latest_snapshot(
                run_id=saved.runs[-1].id, include_interrupted=True
            )
            assert first_snapshot is not None
            assert first_snapshot.state == "interrupted"
            snapshot = await store.latest_snapshot(run_id=resumed.runs[-1].id)
            assert snapshot is not None
            assert snapshot.state == "complete"
            assert snapshot.conversation_id == str(saved.id)
