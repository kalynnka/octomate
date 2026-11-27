"""Generic ACP v1 callbacks through real SRT, with no provider or LLM dependency."""

import json
from collections.abc import AsyncGenerator
from pathlib import Path

import pytest
from acp import schema
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import AsyncEngine
from uuid_utils.compat import uuid7

from octomate import Octomate
from octomate.config import AcpAgentConfig, OctomateConfig
from octomate.habitat.srt import SrtHabitat
from octomate.schemas.thread import ThreadKey
from octomate.tentacles.acp.base import AcpAgentTentacle
from octomate.tentacles.acp.client import AcpProvider
from octomate.tentacles.acp.workspace import AcpWorkspace, workspace
from tests.agent.acp.test_tentacle import custom_provider as custom_provider
from tests.habitat.agents.test_acp_workspace import (
    test_files_lines_and_missing_parents as test_files_lines_and_missing_parents,
)
from tests.habitat.agents.test_acp_workspace import (
    test_rejects_cross_session_outside_and_symlink_paths as test_rejects_cross_session_outside_and_symlink_paths,
)
from tests.habitat.agents.test_acp_workspace import (
    test_running_output_cancelled_wait_kill_and_cleanup as test_running_output_cancelled_wait_kill_and_cleanup,
)
from tests.habitat.agents.test_acp_workspace import (
    test_terminal_exit_environment_stderr_and_release as test_terminal_exit_environment_stderr_and_release,
)
from tests.habitat.agents.test_acp_workspace import (
    test_terminal_start_failure_preserves_diagnostic as test_terminal_start_failure_preserves_diagnostic,
)
from tests.habitat.agents.test_acp_workspace import (
    test_terminals_share_executor_and_cancel_independently as test_terminals_share_executor_and_cancel_independently,
)
from tests.support.invocations import invocation_for
from tests.support.users import a_user, auth_config

pytestmark = pytest.mark.timeout(60)


@pytest.fixture
async def client(habitat: SrtHabitat, tmp_path: Path) -> AsyncGenerator[AcpWorkspace]:
    root = tmp_path / "workspace"
    root.mkdir()
    async with workspace(habitat, str(root)) as client:
        assert client is not None
        client.session_id = str(uuid7())
        yield client
    assert isinstance(client.habitat, SrtHabitat)
    assert not client.habitat.processes


async def test_terminal_cannot_read_sibling_storage(client: AcpWorkspace) -> None:
    sibling = Path(client.cwd).parent / "secret"
    sibling.write_text("PRIVATE_SIBLING_CANARY")
    created = schema.CreateTerminalResponse.model_validate(
        await client.handle(
            "terminal/create",
            {
                "sessionId": client.session_id,
                "command": "/bin/cat",
                "args": [str(sibling)],
            },
        )
    )
    params = {"sessionId": client.session_id, "terminalId": created.terminal_id}
    status = schema.WaitForTerminalExitResponse.model_validate(
        await client.handle("terminal/wait_for_exit", params)
    )
    assert status.exit_code != 0
    output = schema.TerminalOutputResponse.model_validate(
        await client.handle("terminal/output", params)
    )
    assert "PRIVATE_SIBLING_CANARY" not in output.output
    assert "Operation not permitted" in output.output


@pytest.mark.parametrize("callback", ["fs/write_text_file", "terminal/create"])
async def test_independent_tentacle_negotiates_and_serves_callbacks(
    habitat: SrtHabitat,
    custom_provider: AcpProvider,
    tmp_path: Path,
    in_memory_engine: AsyncEngine,
    callback: str,
) -> None:
    app = Octomate(config=OctomateConfig(auth=auth_config()))
    app.volumes.host_storage.root = tmp_path / "volumes"
    app.habitats["native"].config = habitat.config
    custom_provider.env["ACP_TEST_CALLBACK"] = SecretStr(callback)
    agent = app.connect(
        AcpAgentTentacle(
            "custom",
            app,
            config=AcpAgentConfig(provider=custom_provider, gateway=False),
            auth=app.auth,
            habitats=app.habitats["native"],
        )
    )
    user = await a_user("acp-owner")
    sender = await app.users.native_profile(agent.id, user.username)
    assert sender is not None
    sender = await app.users.ensure_profile(agent.id, sender)
    thread = await app.threads.ensure(
        ThreadKey(
            channel_tentacle_id=agent.id,
            chat_type="thread",
            chat_id=str(user.id),
            channel_thread_id=uuid7().hex,
        )
    )
    try:
        async with agent:
            for method in ("session/new", "session/load"):
                async with invocation_for(
                    agent,
                    conversation_address=thread.key.address(sender.channel_user_id),
                    thread_id=thread.id,
                    owner_id=sender.user_id,
                ) as invocation:
                    proof = Path(invocation.cwd) / "proof"
                    custom_provider.env["ACP_TEST_PATH"] = SecretStr(str(proof))
                    result = await agent.run(
                        "Run the scripted client callback",
                        invocation=invocation,
                        conversation_address=thread.key.address(sender.channel_user_id),
                        interactive=False,
                    )
                    if callback == "fs/write_text_file":
                        assert proof.read_text() == "client file"
                assert result.output == "custom harness answer"
                records = [
                    json.loads(line)["message"]
                    for line in (tmp_path / "custom-requests.jsonl")
                    .read_text()
                    .splitlines()
                ]
                capabilities = records[0]["params"]["clientCapabilities"]
                assert capabilities["fs"] == {
                    "readTextFile": True,
                    "writeTextFile": True,
                }
                assert capabilities["terminal"]
                assert any(record.get("method") == method for record in records)
                response = next(
                    record for record in records if record.get("id") == "permission"
                )
                assert "error" not in response
                if callback == "terminal/create":
                    assert response["result"]["terminalId"]
    finally:
        await app.habitats["native"].close()
