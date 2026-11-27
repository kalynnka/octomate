"""Drive the actual DSH ACP provider inside a disposable habitat."""

import asyncio
import json
import os
from contextlib import AsyncExitStack
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import AsyncMock

import pytest
from acp import schema
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
from tests.integration.sbx import remove_sandbox
from tests.support.invocations import invocation_for
from tests.support.users import a_user, auth_config


@pytest.mark.timeout(600)
@pytest.mark.parametrize("resume", [False, True], ids=["tools", "resume"])
async def test_dsh_acp_tools_and_fresh_process_resume(
    in_memory_engine: AsyncEngine,
    tmp_path: Path,
    sbx_cleanup: AsyncExitStack,
    monkeypatch: pytest.MonkeyPatch,
    resume: bool,
) -> None:
    image = os.environ.get("SBX_TEST_IMAGE")
    executable = os.environ.get("DSH_TEST_EXECUTABLE")
    if not image or not executable:
        pytest.fail("Set SBX_TEST_IMAGE and DSH_TEST_EXECUTABLE for live acceptance")
    binary = tmp_path / "bin"
    binary.mkdir()
    (binary / "dsh").symlink_to(executable)
    monkeypatch.setenv("PATH", f"{binary}:{os.environ['PATH']}")
    core_home = tmp_path / "core-dsh"
    core_home.mkdir()
    resolver = await asyncio.create_subprocess_exec(
        "node",
        "--input-type=module",
        "--eval",
        "console.log(import.meta.resolve('@deepseek-ai/dsh-llm'))",
        cwd=(await asyncio.to_thread(Path(executable).resolve)).parent,
        stdout=asyncio.subprocess.PIPE,
    )
    module, _ = await resolver.communicate()
    assert resolver.returncode == 0
    source = (
        Path(__file__).parents[2] / "agent/deepseek/fixtures/dsh_remote_mock.mjs"
    ).read_text()
    source = source[: source.index("export async function apply")]
    source = source.replace(
        '["llm", "approval", "userQuestions", "credentials"]', '["llm"]'
    ).replace(
        "printf OCTOMATE_SMOKE",
        "uname -s > acp-uname.txt; printf acp-proof > acp-proof.txt",
    )
    source += '\nexport function apply(ctx) { ctx.llm.registerAdapter(["octomate-test"], new Mock()); }\n'
    overlay = Path(
        sbx_cleanup.enter_context(TemporaryDirectory(prefix="inky-acp-", dir="/tmp"))
    )
    plugin, patch = overlay / "mock.mjs", overlay / "patch.yaml"
    plugin.write_text(source.replace("__DSH_LLM_MODULE__", module.decode().strip()))
    patch.write_text(
        f"- insert:\n    - id: octomate-smoke\n      name: {plugin}\n"
        "- id: acp\n  config:\n    provider: octomate-test\n    model: mock\n"
    )
    settings = "agent-default-model:\n  provider: octomate-test\n  model: mock\n"
    (core_home / "settings.yaml").write_text(settings)
    host = Octomate(config=OctomateConfig(auth=auth_config()))
    host.habitats["native"].config = SbxHabitatConfig(image=image)
    agent = host.connect(
        AcpAgentTentacle(
            "dsh-acp",
            host,
            auth=host.auth,
            habitats=host.habitats["native"],
            config=AcpAgentConfig(
                gateway=False,
                provider=AcpProvider(
                    command="dsh",
                    args=["acp", "--patch", str(patch)],
                    env={
                        "DSH_HOME": SecretStr(str(core_home)),
                        "DSH_TELEMETRY_DISABLED": SecretStr("1"),
                    },
                ),
            ),
        )
    )
    permission = AsyncMock(
        return_value=schema.RequestPermissionResponse(
            outcome=schema.AllowedOutcome(outcome="selected", option_id="allow-once")
        )
    )
    monkeypatch.setattr(agent, "permission", permission)
    owner = await a_user("dsh-acp-owner")
    sender = await host.users.native_profile(agent.id, owner.username)
    assert sender is not None
    sender = await host.users.ensure_profile(agent.id, sender)
    thread = await host.threads.ensure(
        ThreadKey(
            channel_tentacle_id=agent.id,
            chat_type="thread",
            chat_id=str(owner.id),
            channel_thread_id=uuid7().hex,
        )
    )
    address = thread.key.address(sender.channel_user_id)
    scope = InvocationScope(user_id=owner.id, conversation_id=uuid7())
    sandbox = host.habitats["native"].residence(scope).habitat
    assert isinstance(sandbox, SbxHabitat)
    sbx_cleanup.push_async_callback(remove_sandbox, sandbox)
    sbx_cleanup.push_async_callback(host.habitats["native"].close)
    async with host.habitats["native"].enter(scope, uuid7()):
        guest_module = await sandbox.control(
            "exec",
            "--workdir=/opt/dsh",
            sandbox.id,
            "node",
            "--input-type=module",
            "--eval",
            "console.log(import.meta.resolve('@deepseek-ai/dsh-llm'))",
        )
        await sandbox.control(
            "exec",
            sandbox.id,
            "python",
            "-c",
            "import json,sys; from pathlib import Path; "
            "[(p.parent.mkdir(parents=True, exist_ok=True),p.write_text(text)) "
            "for name,text in json.loads(sys.argv[1]).items() for p in [Path(name)]]",
            json.dumps(
                {
                    str(patch): patch.read_text(),
                    str(plugin): source.replace(
                        "__DSH_LLM_MODULE__", guest_module.decode().strip()
                    ),
                    str(sandbox.config_dir / ".dsh/settings.yaml"): settings,
                }
            ),
        )
        async with agent:
            session_id = None
            prompts = ["Run the probe"]
            if resume:
                prompts.append("Resume and run the probe again")
            for prompt in prompts:
                async with invocation_for(
                    agent,
                    conversation_address=address,
                    thread_id=thread.id,
                    owner_id=owner.id,
                ) as invocation:
                    result = await agent.run(
                        prompt,
                        invocation=invocation,
                        conversation_address=address,
                        interactive=False,
                    )
                    assert result.output == "Octomate Remote API works"
                    assert (
                        invocation.workspace.path / "acp-proof.txt"
                    ).read_text() == "acp-proof"
                    assert (
                        invocation.workspace.path / "acp-uname.txt"
                    ).read_text().strip() == "Linux"
                    conversation = await host.conversations.get(
                        invocation.conversation.id, with_runs=False
                    )
                    assert conversation.external_id is not None
                    if session_id is not None:
                        assert conversation.external_id == session_id
                    session_id = conversation.external_id
