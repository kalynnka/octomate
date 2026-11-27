"""Real Codex tools under SRT, driven by a local keyless Responses fixture."""

import json
import os
from pathlib import Path
from unittest.mock import AsyncMock

import anyio
import pytest
from httpx2 import URL
from openai_codex import AsyncThread
from openai_codex import CodexConfig as RuntimeConfig
from openai_codex.api import Sandbox
from openai_codex.generated.v2_all import ThreadUnsubscribeResponse
from uuid_utils.compat import uuid7

from octomate.config.agents import CodexConfig
from octomate.habitat.srt import SrtHabitat
from octomate.tentacles.codex.client import SharedCodex
from octomate.tentacles.codex.ink import DRIVEN_CONFIG_OVERRIDES, CodexInk
from tests.integration.native_sandbox.provider import ScriptedProvider, local_provider

pytestmark = pytest.mark.timeout(60)


async def test_codex_external_sandbox_survives_resume_and_fork(
    habitat: SrtHabitat,
) -> None:
    executable = os.environ.get("CODEX_TEST_EXECUTABLE")
    if executable is None:
        pytest.skip("Set CODEX_TEST_EXECUTABLE to the native Codex binary")
    binary = Path(await anyio.Path(executable).resolve(strict=True))
    await habitat.stop()
    habitat.config = habitat.config.model_copy(update={"read_paths": [binary.parent]})
    await habitat.prepare()
    home = habitat.root / "codex-home"
    home.mkdir()
    workspace = habitat.root / "workspace"
    workspace.mkdir()
    state = ScriptedProvider(
        "responses",
        tool="exec_command",
        arguments={
            "cmd": "printf inside > proof; cat proof; "
            "if cat /private/etc/passwd >/dev/null; "
            "then printf OUTSIDE_ALLOWED; else printf OUTSIDE_DENIED; fi",
            "max_output_tokens": 1000,
        },
    )
    handler = AsyncMock(side_effect=AssertionError("Unexpected approval request"))
    with local_provider(state) as url:
        runtime = RuntimeConfig(
            codex_bin=str(binary),
            cwd=str(workspace),
            env={"CODEX_HOME": str(home), "OPENAI_API_KEY": ""},
            config_overrides=(
                *DRIVEN_CONFIG_OVERRIDES,
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
            handler=handler,
        )
        async with SharedCodex(
            runtime, handler, instrument=False, habitat=habitat
        ) as client:
            ink.pool.core_client = client
            thread, _ = await ink.start_thread(
                client=client,
                approval_mode=None,
                base_instructions=None,
                config={},
                cwd=str(workspace),
                developer_instructions=None,
                ephemeral=False,
                model="gpt-5",
                model_provider="audit",
                personality=None,
                sandbox=Sandbox.workspace_write,
            )

            async def exercise(current: AsyncThread) -> None:
                state.call_id = str(uuid7())
                state.outcome = None
                turn = await ink.start_turn(
                    current,
                    "Execute the synthetic probe once.",
                    approval_mode=None,
                    sandbox=Sandbox.workspace_write,
                    cwd=str(workspace),
                    effort=None,
                    model=None,
                    output_schema=None,
                    personality=None,
                    summary=None,
                )
                await turn.run()
                assert state.outcome is not None
                assert "inside" in state.outcome.text
                assert "OUTSIDE_DENIED" in state.outcome.text
                assert "OUTSIDE_ALLOWED" not in state.outcome.text

            await exercise(thread)
            await exercise(thread)
            original_id = thread.id
            await client._client.request(
                "thread/unsubscribe",
                {"threadId": original_id},
                response_model=ThreadUnsubscribeResponse,
            )
            resumed, _ = await ink.resume_thread(
                client=client,
                thread_id=original_id,
                approval_mode=None,
                base_instructions=None,
                config={},
                cwd=str(workspace),
                developer_instructions=None,
                model="gpt-5",
                model_provider="audit",
                personality=None,
                sandbox=Sandbox.workspace_write,
            )
            assert resumed.id == original_id
            await exercise(resumed)
            fork_id = await ink.fork_session(resumed.id, cwd=workspace)
            assert fork_id != original_id
            await exercise(AsyncThread(client, fork_id))
            handler.assert_not_awaited()
