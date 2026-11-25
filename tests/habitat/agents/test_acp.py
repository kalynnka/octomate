"""ACP uses the habitat's process, paths and callback address."""

import asyncio
import json
import os
import sys
from pathlib import Path, PurePosixPath
from unittest.mock import AsyncMock

import acp
import pytest
from acp import schema
from acp.utils import notify_model, request_model
from pydantic import HttpUrl
from sqlalchemy.ext.asyncio import AsyncEngine
from uuid_utils.compat import uuid7

from octomate import Octomate
from octomate.config import AcpAgentConfig, OctomateConfig
from octomate.config.habitat import SbxHabitatConfig
from octomate.habitat.sbx import SbxHabitat
from octomate.schemas.thread import ThreadKey
from octomate.schemas.volumes import Volume
from octomate.tentacles.acp.base import AcpAgentTentacle
from octomate.tentacles.acp.client import AcpProvider, NativeAcpClient
from tests.agent.acp.test_client import Callbacks
from tests.agent.acp.test_tentacle import custom_provider as custom_provider
from tests.habitat.agents.test_claude import executable as executable
from tests.habitat.test_contracts import IMAGE
from tests.support.invocations import invocation_for
from tests.support.users import a_user, auth_config


@pytest.mark.parametrize("cancel", [False, True])
async def test_guest_stream_permissions_cancellation_and_cleanup(
    executable: Path, custom_provider: AcpProvider, tmp_path: Path, cancel: bool
) -> None:
    habitat = SbxHabitat(
        config=SbxHabitatConfig(image=IMAGE, executable=str(executable)),
        user_id=uuid7(),
        root=tmp_path,
        config_dir=tmp_path,
    )
    callbacks = Callbacks()
    alias = tmp_path / "guest-alias"
    alias.symlink_to(tmp_path, target_is_directory=True)
    async with (
        asyncio.timeout(10),
        NativeAcpClient.connect(
            custom_provider,
            callbacks,
            schema.InitializeRequest(protocol_version=1),
            habitat=habitat,
            cwd=str(tmp_path),
        ) as client,
    ):
        session = await client.new_session(
            schema.NewSessionRequest(cwd=str(alias), mcp_servers=[])
        )
        assert session.cwd == str(alias)
        with pytest.raises(ValueError, match="absolute"):
            await client.new_session(
                schema.NewSessionRequest(cwd="relative", mcp_servers=[])
            )
        prompt = asyncio.create_task(
            request_model(
                client.connection,
                "session/prompt",
                schema.PromptRequest(
                    session_id=session.session_id, prompt=[acp.text_block("hello")]
                ),
                schema.PromptResponse,
            )
        )
        await callbacks.asked.wait()
        assert not prompt.done()
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
    records = [
        json.loads(line)
        for line in (tmp_path / "custom-requests.jsonl").read_text().splitlines()
    ]
    assert records[1]["message"]["params"]["cwd"] == str(alias)
    with pytest.raises(ProcessLookupError):
        os.kill(records[0]["pid"], 0)


async def test_guest_startup_failure_preserves_stderr(
    executable: Path, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    habitat = SbxHabitat(
        config=SbxHabitatConfig(image=IMAGE, executable=str(executable)),
        user_id=uuid7(),
        root=tmp_path,
        config_dir=tmp_path,
    )
    provider = AcpProvider(
        command=sys.executable,
        args=[
            "-c",
            "import sys; print('acp-startup-failed', file=sys.stderr); sys.exit(7)",
        ],
    )
    with pytest.raises(ConnectionError):
        async with (
            asyncio.timeout(10),
            NativeAcpClient.connect(
                provider,
                Callbacks(),
                schema.InitializeRequest(protocol_version=1),
                habitat=habitat,
                cwd=str(tmp_path),
            ),
        ):
            pytest.fail("Failed provider must not initialize")
    assert "acp-startup-failed" in caplog.text
    assert "exit 7" in caplog.text


async def test_turns_use_guest_paths_home_mcp_and_resume(
    executable: Path,
    custom_provider: AcpProvider,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    in_memory_engine: AsyncEngine,
) -> None:
    app = Octomate(config=OctomateConfig(auth=auth_config()))
    app.habitats.config = SbxHabitatConfig(
        image=IMAGE,
        executable=str(executable),
        core_address=HttpUrl("https://core.example:9443"),
    )
    monkeypatch.setattr(SbxHabitat, "prepare", AsyncMock())
    monkeypatch.setattr(SbxHabitat, "stop", AsyncMock())
    guest = tmp_path / "guest"
    for part in ("tree", "config", "cache"):
        (guest / part).mkdir(parents=True)

    def guest_path(habitat: SbxHabitat, volume: Volume) -> PurePosixPath:
        assert volume.usage is not None
        return PurePosixPath(guest / volume.usage)

    monkeypatch.setattr(SbxHabitat, "path", guest_path)
    agent = app.connect(
        AcpAgentTentacle(
            "custom",
            app,
            config=AcpAgentConfig(provider=custom_provider),
            auth=app.auth,
            habitats=app.habitats,
        )
    )
    user = await a_user("alice")
    sender = await app.users.native_profile(agent.id, user.username)
    assert sender is not None
    sender = await app.users.ensure_profile(agent.id, sender)
    thread = await app.threads.ensure(
        ThreadKey(agent.id, "thread", str(user.id), uuid7().hex)
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
                    result = await agent.run(
                        "hello",
                        invocation=invocation,
                        conversation_address=thread.key.address(sender.channel_user_id),
                        interactive=False,
                    )
                assert result.output == "custom harness answer"
                records = [
                    json.loads(line)
                    for line in (tmp_path / "custom-requests.jsonl")
                    .read_text()
                    .splitlines()
                ]
                assert records[0]["home"] == str(guest / "config")
                assert records[0]["codex_home"] == str(guest / "config" / ".codex")
                assert records[0]["cwd"] == str(guest / "tree")
                messages = [record["message"] for record in records]
                request = next(
                    item["params"] for item in messages if item.get("method") == method
                )
                assert request["cwd"] == str(guest / "tree")
                assert (
                    request["mcpServers"][0]["url"]
                    == "https://host.docker.internal:9443/octomate/mcp"
                )
    finally:
        await app.habitats.close()


async def test_cancelling_guest_connection_reaps_process(
    executable: Path,
    custom_provider: AcpProvider,
    tmp_path: Path,
) -> None:
    habitat = SbxHabitat(
        config=SbxHabitatConfig(image=IMAGE, executable=str(executable)),
        user_id=uuid7(),
        root=tmp_path,
        config_dir=tmp_path,
    )
    connected = asyncio.Event()

    async def run() -> None:
        async with NativeAcpClient.connect(
            custom_provider,
            Callbacks(),
            schema.InitializeRequest(protocol_version=1),
            habitat=habitat,
            cwd=str(tmp_path),
        ):
            connected.set()
            await asyncio.Event().wait()

    async with asyncio.timeout(10), asyncio.TaskGroup() as tasks:
        owner = tasks.create_task(run())
        await connected.wait()
        owner.cancel()
        with pytest.raises(asyncio.CancelledError):
            await owner
    record = json.loads((tmp_path / "custom-requests.jsonl").read_text())
    with pytest.raises(ProcessLookupError):
        os.kill(record["pid"], 0)
