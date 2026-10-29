"""Drive the real tentacles with deterministic, keyless model tool requests."""

import asyncio
import json
import os
import socket
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal

import anyio
import pytest
from openai_codex import CodexConfig as RuntimeConfig
from pydantic import UUID7, TypeAdapter
from pydantic_ai.messages import (
    ModelMessage,
    ModelRequest,
    RetryPromptPart,
    ToolReturnPart,
)
from pydantic_ai.models.function import (
    AgentInfo,
    DeltaToolCall,
    DeltaToolCalls,
    FunctionModel,
)
from pydantic_ai.tools import DeferredToolRequests

from octomate import Octomate
from octomate.capabilities.harness.agent import Agent
from octomate.config.agents import ClaudeCodeConfig, CodexConfig, DeepseekConfig
from octomate.schemas.conversation import ChannelAddress
from octomate.tentacles.claude import ClaudeCodeTentacle
from octomate.tentacles.codex import CodexTentacle
from octomate.tentacles.deepseek import DeepseekTentacle
from octomate.tentacles.inkling import InklingTentacle
from octomate.tentacles.inkling.base import InklingOutput
from octomate.types.json import JsonObject
from tests.integration.native_sandbox.matrix import NativeCall, ToolOutcome
from tests.integration.native_sandbox.provider import ScriptedProvider, local_provider

ADDRESS = ChannelAddress(
    channel_tentacle_id="sandbox-audit",
    chat_type="thread",
    chat_id="synthetic",
    channel_thread_id="audit",
    user_id="synthetic",
)


@asynccontextmanager
async def inkling_runner(
    octomate: Octomate, thread_id: UUID7
) -> AsyncIterator[NativeCall]:
    async def call(kind: Literal["shell", "file"], argument: str) -> ToolOutcome:
        outcome: ToolOutcome | None = None

        async def respond(
            messages: list[ModelMessage], info: AgentInfo
        ) -> AsyncIterator[str | DeltaToolCalls]:
            nonlocal outcome
            last = messages[-1]
            assert isinstance(last, ModelRequest)
            returned = [
                part
                for part in last.parts
                if isinstance(part, ToolReturnPart | RetryPromptPart)
            ]
            if returned:
                part = returned[-1]
                outcome = (
                    ToolOutcome(part.model_response(), True)
                    if isinstance(part, RetryPromptPart)
                    else ToolOutcome(part.model_response_str())
                )
                yield "done"
                return
            tool = "run_command" if kind == "shell" else "read_file"
            assert tool in {entry.name for entry in info.function_tools}
            args = {"command": argument} if kind == "shell" else {"path": argument}
            yield {
                0: DeltaToolCall(
                    name=tool, json_args=json.dumps(args), tool_call_id="audit"
                )
            }

        output: list[type[str] | type[DeferredToolRequests]] = [
            str,
            DeferredToolRequests,
        ]
        agent: Agent[None, InklingOutput] = Agent(
            FunctionModel(stream_function=respond),
            deps_type=type(None),
            output_type=output,
        )
        tentacle = InklingTentacle(
            "inkling", octomate, agent=agent, permission_mode="bypassPermissions"
        )
        await tentacle.run(
            "Execute the synthetic probe once.",
            conversation_address=ADDRESS,
            thread_id=thread_id,
            output_type=output,
        )
        assert outcome is not None, "the native tool never returned a result"
        return outcome

    yield call


@asynccontextmanager
async def codex_runner(
    octomate: Octomate,
    thread_id: UUID7,
    tmp_path: Path,
) -> AsyncIterator[NativeCall]:
    state = ScriptedProvider("responses")
    home = tmp_path / "codex-home"
    await anyio.Path(home).mkdir()
    with local_provider(state) as url:
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
        tentacle = CodexTentacle(
            "codex",
            octomate,
            config=CodexConfig(runtime=runtime),
            commands=octomate.commands,
            projects=octomate.projects,
            threads=octomate.threads,
            files=octomate.files,
            conversations=octomate.conversations,
            deferred_actions=octomate.deferred_actions,
            workspaces=octomate.workspaces,
            users=octomate.users,
            bearers=octomate.bearers,
            auth=octomate.auth,
            gateway_manager=octomate.gateway,
        )
        async with tentacle:

            async def call(
                kind: Literal["shell", "file"], argument: str
            ) -> ToolOutcome:
                if kind == "file":
                    pytest.skip(
                        "Codex has no separate native file tool; filesystem access is covered through exec"
                    )
                state.tool = "exec_command"
                state.arguments = {"cmd": argument, "max_output_tokens": 4000}
                await tentacle.run(
                    "Execute the synthetic probe once.",
                    conversation_address=ADDRESS,
                    thread_id=thread_id,
                    interactive=False,
                )
                assert state.outcome is not None, (
                    "Codex never returned its native tool result"
                )
                return state.outcome

            yield call


@asynccontextmanager
async def claude_runner(
    octomate: Octomate,
    thread_id: UUID7,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncIterator[NativeCall]:
    state = ScriptedProvider("anthropic")
    home = tmp_path / "claude-home"
    await anyio.Path(home).mkdir()
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(home))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "synthetic-provider-key")
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
    monkeypatch.setenv("CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC", "1")
    with local_provider(state) as url:
        monkeypatch.setenv("ANTHROPIC_BASE_URL", url)
        tentacle = ClaudeCodeTentacle(
            "claude",
            octomate,
            config=ClaudeCodeConfig(permission_mode="bypassPermissions"),
            commands=octomate.commands,
            projects=octomate.projects,
            threads=octomate.threads,
            files=octomate.files,
            conversations=octomate.conversations,
            deferred_actions=octomate.deferred_actions,
            workspaces=octomate.workspaces,
            users=octomate.users,
            bearers=octomate.bearers,
            mcp=octomate.mcp,
        )
        try:

            async def call(
                kind: Literal["shell", "file"], argument: str
            ) -> ToolOutcome:
                state.tool = "Bash" if kind == "shell" else "Read"
                state.arguments = (
                    {
                        "command": argument,
                        "timeout": 10000,
                        "dangerouslyDisableSandbox": True,
                    }
                    if kind == "shell"
                    else {"file_path": argument}
                )
                await tentacle.run(
                    "Execute the synthetic probe once.",
                    conversation_address=ADDRESS,
                    thread_id=thread_id,
                    model="claude-sonnet-4-6",
                )
                assert state.outcome is not None, (
                    "Claude never returned its native tool result"
                )
                return state.outcome

            yield call
        finally:
            await tentacle.ink.close()
            await tentacle.session_tailer.shutdown()


@asynccontextmanager
async def deepseek_runner(
    octomate: Octomate,
    thread_id: UUID7,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncIterator[NativeCall]:
    executable = os.environ.get("DSH_TEST_EXECUTABLE")
    if executable is None:
        pytest.skip(
            "set DSH_TEST_EXECUTABLE to an installed/built dsh with the supported Remote API"
        )
    resolver = await asyncio.create_subprocess_exec(
        "node",
        "--input-type=module",
        "--eval",
        "console.log(import.meta.resolve('@deepseek-ai/dsh-llm'))",
        cwd=(await anyio.Path(executable).resolve()).parent,
        stdout=asyncio.subprocess.PIPE,
    )
    module, _ = await resolver.communicate()
    assert resolver.returncode == 0
    home = tmp_path / "dsh-home"
    await anyio.Path(home).mkdir()
    help_process = await asyncio.create_subprocess_exec(
        executable,
        "web",
        "--help",
        env={**os.environ, "DSH_HOME": str(home)},
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    help_output, help_error = await help_process.communicate()
    assert help_process.returncode == 0, help_error.decode()
    if b"--no-open" not in help_output:
        pytest.skip("this dsh cannot suppress browser startup with --no-open")
    await anyio.Path(home / "settings.yaml").write_text(
        "agent-default-model:\n  provider: octomate-test\n  model: mock\n"
    )
    template = await anyio.Path(
        Path(__file__).with_name("deepseek_provider.mjs")
    ).read_text()
    result_path = tmp_path / "native-result.json"
    plugin = tmp_path / "provider.mjs"
    patch = tmp_path / "patch.yml"
    await anyio.Path(patch).write_text(
        f"- insert:\n    - id: sandbox-audit\n      name: {plugin}\n"
    )
    monkeypatch.setenv("DSH_TELEMETRY_DISABLED", "1")
    monkeypatch.setenv("DSH_AGENTS_HOME", str(tmp_path / "agents"))
    with socket.socket() as reservation:
        reservation.bind(("127.0.0.1", 0))
        port = reservation.getsockname()[1]
    tentacle = DeepseekTentacle(
        "deepseek",
        octomate,
        config=DeepseekConfig(
            executable=executable,
            port=port,
            dsh_home=home,
            extra_args=["--patch", str(patch)],
        ),
    )

    async def call(kind: Literal["shell", "file"], argument: str) -> ToolOutcome:
        tool = "bash" if kind == "shell" else "read"
        args: JsonObject = (
            {"command": argument, "description": "Synthetic sandbox probe"}
            if kind == "shell"
            else {"file_path": argument}
        )
        source = (
            template.replace("__DSH_LLM_MODULE__", module.decode().strip())
            .replace(
                "__AUDIT_CALL__",
                json.dumps({"name": tool, "arguments": json.dumps(args)}),
            )
            .replace("__AUDIT_RESULT__", json.dumps(str(result_path)))
        )
        await anyio.Path(plugin).write_text(source)
        async with tentacle:
            await tentacle.run(
                "Execute the synthetic probe once.",
                conversation_address=ADDRESS,
                thread_id=thread_id,
                model="octomate-test:mock",
                interactive=False,
            )
        return TypeAdapter(ToolOutcome).validate_json(
            await anyio.Path(result_path).read_text()
        )

    yield call
