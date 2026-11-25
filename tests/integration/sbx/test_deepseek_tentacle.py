"""Real DSH web transport, resumed turns, and per-owner VM boundaries."""

import asyncio
import json
import os
from base64 import b64decode
from contextlib import AsyncExitStack
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import AsyncMock

import pytest
from octomate_protocol.deepseek import OkResult
from openai_codex import CodexConfig
from openai_codex.generated.v2_all import FsReadFileResponse
from sqlalchemy.ext.asyncio import AsyncEngine
from uuid_utils.compat import uuid7

from octomate import Octomate
from octomate.config import OctomateConfig
from octomate.config.agents import DeepseekConfig
from octomate.config.habitat import SbxHabitatConfig
from octomate.habitat.sbx import SbxHabitat
from octomate.managers.habitat import Invocation
from octomate.schemas.conversation import ChannelAddress
from octomate.schemas.habitat import InvocationScope
from octomate.schemas.thread import ThreadKey
from octomate.tentacles.codex.client import SharedCodex
from octomate.tentacles.deepseek import DeepseekTentacle
from tests.integration.sbx import cli_pids, remove_sandbox
from tests.support.invocations import invocation_for
from tests.support.users import a_user, auth_config


@pytest.mark.timeout(600)
async def test_dsh_turns_resume_share_codex_vm_and_isolate_other_owner(
    in_memory_engine: AsyncEngine,
    sbx_cleanup: AsyncExitStack,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    image = os.environ.get("SBX_TEST_IMAGE")
    executable = os.environ.get("DSH_TEST_EXECUTABLE")
    if not image or not executable:
        pytest.fail("Set SBX_TEST_IMAGE and DSH_TEST_EXECUTABLE for live acceptance")
    for key in list(os.environ):
        if key.startswith(("DSH_", "DEEPSEEK_")):
            monkeypatch.delenv(key)
    binary = tmp_path / "bin"
    binary.mkdir()
    (binary / "dsh").symlink_to(executable)
    monkeypatch.setenv("PATH", f"{binary}:{os.environ['PATH']}")
    monkeypatch.setenv("DSH_TELEMETRY_DISABLED", "1")
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
    fixture = (
        Path(__file__).parents[2] / "agent/deepseek/fixtures/dsh_remote_mock.mjs"
    ).read_text()
    fixture = fixture.replace(
        "printf OCTOMATE_SMOKE",
        "uname -s; printf habitat-proof > proof.txt; cat proof.txt",
    )
    host = Octomate(config=OctomateConfig(auth=auth_config()))
    host.habitats.config = SbxHabitatConfig(image=image)
    owner, other = await a_user("dsh-owner"), await a_user("dsh-other")
    owner_scope = InvocationScope(user_id=owner.id, conversation_id=uuid7())
    other_scope = InvocationScope(user_id=other.id, conversation_id=uuid7())
    sandbox = host.habitats.residence(owner_scope).habitat
    peer = host.habitats.residence(other_scope).habitat
    assert isinstance(sandbox, SbxHabitat)
    assert isinstance(peer, SbxHabitat)
    sbx_cleanup.push_async_callback(remove_sandbox, sandbox)
    sbx_cleanup.push_async_callback(remove_sandbox, peer)
    sbx_cleanup.push_async_callback(host.habitats.close)
    core_home = tmp_path / "core-dsh"
    core_home.mkdir()
    credentials = "version: 1\nrefs:\n  OCTOMATE_DSH_TEST_TOKEN: synthetic\n"
    (core_home / ".credentials.yaml").write_text(credentials)
    (core_home / ".credentials.yaml").chmod(0o600)
    settings = "agent-default-model:\n  provider: octomate-test\n  model: mock\n"
    (core_home / "settings.yaml").write_text(settings)
    address = ChannelAddress("im", "thread", "dsh-owner", user_id="dsh-owner")
    thread = await host.threads.ensure(ThreadKey.from_address(address))

    # The same configured overlay path exists independently in core and each VM.
    overlay = sbx_cleanup.enter_context(
        TemporaryDirectory(prefix="inky-dsh-", dir="/tmp")
    )
    patch = Path(overlay) / "patch.yaml"
    plugin = Path(overlay) / "mock.mjs"
    plugin.write_text(fixture.replace("__DSH_LLM_MODULE__", module.decode().strip()))
    patch.write_text(f"- insert:\n    - id: octomate-smoke\n      name: {plugin}\n")
    tentacle = DeepseekTentacle(
        host,
        auth=host.auth,
        habitats=host.habitats,
        config=DeepseekConfig(
            port=0, dsh_home=core_home, extra_args=["--patch", str(patch)]
        ),
    )
    approval = AsyncMock(return_value=OkResult(value="allowed-once"))
    question = AsyncMock(
        return_value=OkResult(
            value={
                "answers": [
                    {"id": "smoke-question", "selected": [], "custom": "continue"}
                ]
            }
        )
    )
    monkeypatch.setattr(tentacle, "answer_approval", approval)
    monkeypatch.setattr(tentacle, "answer_questions", question)
    async with tentacle, host.habitats.enter(owner_scope, uuid7()):
        guest_module = await sandbox.control(
            "exec",
            "--workdir=/opt/dsh",
            sandbox.name,
            "node",
            "--input-type=module",
            "--eval",
            "console.log(import.meta.resolve('@deepseek-ai/dsh-llm'))",
        )
        guest_home = sandbox.config_dir / ".dsh"
        await sandbox.control(
            "exec",
            sandbox.name,
            "python",
            "-c",
            "import json,sys; from pathlib import Path; "
            "files=json.loads(sys.argv[1]); "
            "[(p.parent.mkdir(parents=True, exist_ok=True), p.write_text(text), p.chmod(0o600)) "
            "for name,text in files.items() for p in [Path(name)]]",
            json.dumps(
                {
                    str(patch): patch.read_text(),
                    str(plugin): fixture.replace(
                        "__DSH_LLM_MODULE__", guest_module.decode().strip()
                    ),
                    str(guest_home / ".credentials.yaml"): credentials,
                    str(guest_home / "settings.yaml"): settings,
                }
            ),
        )
        connection = None
        session_id = None
        pids: list[int] = []
        for prompt in ("Run the probe", "Resume and run it again"):
            async with invocation_for(
                tentacle,
                conversation_address=address,
                thread_id=thread.id,
                owner_id=owner.id,
            ) as invocation:
                assert invocation.habitat is sandbox
                result = await tentacle.run(
                    prompt,
                    conversation_address=address,
                    invocation=invocation,
                    model="octomate-test:mock",
                )
                assert result.output == "Octomate Remote API works"
                assert (
                    invocation.workspace.path / "proof.txt"
                ).read_text() == "habitat-proof"
                current = await tentacle.ink.acquire(
                    sandbox, env=invocation.environment
                )
                current_pids = await cli_pids(sandbox, argument_pattern=".*/ds[h]")
                assert len(current_pids) == 1
                assert connection is None or current is connection
                assert not pids or pids == current_pids
                conversation = await host.conversations.get(invocation.conversation.id)
                assert conversation.external_id is not None
                assert session_id is None or session_id == conversation.external_id
                connection, session_id, pids = (
                    current,
                    conversation.external_id,
                    current_pids,
                )
                await assert_harness_sharing(host, invocation, other_scope)
        assert len(conversation.runs) == 2
        assert approval.await_count == question.await_count == 2
        assert not list(core_home.glob("sessions/**/*"))
        assert await cli_pids(sandbox, argument_pattern=".*/ds[h]") == pids
    assert not await cli_pids(sandbox, argument_pattern=".*/ds[h]")
    assert json.loads(await sandbox.control("ports", sandbox.name, "--json")) == []


async def assert_harness_sharing(
    host: Octomate, invocation: Invocation, other_scope: InvocationScope
) -> None:
    sandbox = invocation.habitat
    peer = host.habitats.residence(other_scope).habitat
    assert isinstance(sandbox, SbxHabitat)
    assert isinstance(peer, SbxHabitat)
    # A second harness reads the DSH-created file inside the very same VM.
    async with SharedCodex(
        CodexConfig(
            codex_bin="codex",
            cwd=str(invocation.cwd),
            env={"CODEX_HOME": invocation.environment["CODEX_HOME"]},
        ),
        AsyncMock(return_value={"decision": "decline"}),
        instrument=False,
        habitat=sandbox,
    ) as codex:
        content = await codex._client.request(
            "fs/readFile",
            {"path": str(invocation.cwd / "proof.txt")},
            response_model=FsReadFileResponse,
        )
        assert b64decode(content.data_base64) == b"habitat-proof"
        async with host.habitats.enter(other_scope, uuid7()):
            assert peer.name != sandbox.name
            hidden = await peer.control(
                "exec",
                peer.name,
                "python",
                "-c",
                "from pathlib import Path; import sys; print(Path(sys.argv[1]).exists())",
                str(invocation.cwd / "proof.txt"),
            )
            assert hidden.strip() == b"False"
            assert await cli_pids(peer, argument_pattern=".*/ds[h]") == []
            assert await cli_pids(peer, argument_pattern="app-serve[r]") == []
