"""Native bindings and bidirectional RPC over a real, scripted stdio process."""

from __future__ import annotations

import asyncio
import json
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path

import acp
import pytest
from acp import schema
from acp.utils import notify_model, request_model
from anyio import Path as AsyncPath
from pydantic import JsonValue

from octomate.tentacles.acp.client import AcpProvider, NativeAcpClient
from octomate.types.threads import CLAUDE_NATIVE_ID, CODEX_NATIVE_ID


@dataclass
class Callbacks:
    updates: list[schema.SessionNotification] = field(default_factory=list)
    permissions: list[schema.RequestPermissionRequest] = field(default_factory=list)
    asked: asyncio.Event = field(default_factory=asyncio.Event)
    answered: asyncio.Event = field(default_factory=asyncio.Event)

    async def __call__(
        self, method: str, params: JsonValue | None, notification: bool
    ) -> JsonValue | None:
        if notification and method == "session/update":
            self.updates.append(schema.SessionNotification.model_validate(params))
            return None
        if not notification and method == "session/request_permission":
            self.permissions.append(
                schema.RequestPermissionRequest.model_validate(params)
            )
            self.asked.set()
            await self.answered.wait()
            return {"outcome": {"outcome": "selected", "optionId": "yes"}}
        raise acp.RequestError.method_not_found(method)


@pytest.fixture
def adapter_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> dict[str, str]:
    source = Path(__file__).with_name("fixtures").joinpath("acp_agent.py").read_text()
    for command in ("claude-agent-acp", "codex-acp"):
        executable = tmp_path / command
        executable.write_text(f"#!{sys.executable}\n{source}")
        executable.chmod(0o700)
    monkeypatch.setenv("PATH", f"{tmp_path}{os.pathsep}{os.defpath}")
    return {"ACP_TEST_LOG": str(tmp_path / "requests.jsonl")}


@pytest.mark.parametrize(
    ("provider", "native_id"),
    [(AcpProvider.CLAUDE, CLAUDE_NATIVE_ID), (AcpProvider.CODEX, CODEX_NATIVE_ID)],
)
async def test_native_session_preserves_workspace_mcp_and_extensions(
    adapter_environment: dict[str, str],
    provider: AcpProvider,
    native_id: str,
) -> None:
    initialize = schema.InitializeRequest(
        protocol_version=1,
        client_capabilities=schema.ClientCapabilities(),
        field_meta={"protocol_version": "extension", "client": "octomate"},
    )
    request = schema.NewSessionRequest(
        cwd="/workspace/../project",
        additional_directories=["/second-root"],
        mcp_servers=[
            schema.HttpMcpServer(
                type="http",
                name="web-tools",
                url="https://EXAMPLE.com/mcp?token=a%2fb",
                headers=[
                    schema.HttpHeader(name="Authorization", value="Bearer fixture")
                ],
            ),
        ],
        field_meta={"cwd": "extension, not the workspace", "nested": {"flag": True}},
    )
    async with NativeAcpClient.connect(
        provider, Callbacks(), initialize, env=adapter_environment
    ) as client:
        first = await client.new_session(request)
        second = await client.new_session(request)
        assert first.provider is provider
        assert first.key.kind == "native_thread"
        assert first.key.channel_tentacle_id == native_id
        assert first.key.chat_id == first.response.session_id == "native-1"
        assert second.key.chat_id == "native-2"
        assert first.cwd == "/project"
        assert first.response.field_meta == {"providerSession": True}
        capabilities = client.initialization.agent_capabilities
        assert capabilities is not None
        assert capabilities.load_session is True
        assert client.initialization.field_meta == {"peer": "retained"}

    log = await AsyncPath(adapter_environment["ACP_TEST_LOG"]).read_text()
    records = [json.loads(line) for line in log.splitlines()]
    expected = initialize.model_dump(
        mode="json", by_alias=True, exclude_none=True, exclude_defaults=True
    )
    assert records[0]["message"]["params"] == expected
    assert records[1]["message"]["params"] == {
        **request.model_dump(
            mode="json", by_alias=True, exclude_none=True, exclude_defaults=True
        ),
        "cwd": "/project",
    }
    assert request.cwd == "/workspace/../project"
    with pytest.raises(ProcessLookupError):
        os.kill(records[0]["pid"], 0)


@pytest.mark.parametrize("provider", list(AcpProvider))
async def test_session_resolves_workspace_aliases_without_changing_request(
    adapter_environment: dict[str, str], provider: AcpProvider, tmp_path: Path
) -> None:
    workspace = tmp_path / "workspace"
    await AsyncPath(workspace).mkdir()
    alias = tmp_path / "alias"
    await AsyncPath(alias).symlink_to(workspace, target_is_directory=True)
    request = schema.NewSessionRequest(
        cwd=str(alias), additional_directories=[str(alias)], mcp_servers=[]
    )
    async with NativeAcpClient.connect(
        provider,
        Callbacks(),
        schema.InitializeRequest(protocol_version=1),
        env=adapter_environment,
    ) as client:
        session = await client.new_session(request)
    log = await AsyncPath(adapter_environment["ACP_TEST_LOG"]).read_text()
    records = [json.loads(line) for line in log.splitlines()]
    assert session.cwd == str(await AsyncPath(workspace).resolve())
    assert records[1]["message"]["params"]["cwd"] == session.cwd
    assert records[1]["message"]["params"]["additionalDirectories"] == [session.cwd]
    assert request.cwd == str(alias)
    assert request.additional_directories == [str(alias)]


@pytest.mark.parametrize("provider", list(AcpProvider))
@pytest.mark.parametrize("cancel", [False, True])
async def test_updates_and_control_continue_while_permission_is_pending(
    adapter_environment: dict[str, str], provider: AcpProvider, cancel: bool
) -> None:
    callbacks = Callbacks()
    async with (
        asyncio.timeout(10),
        NativeAcpClient.connect(
            provider,
            callbacks,
            schema.InitializeRequest(protocol_version=1),
            env=adapter_environment,
        ) as client,
    ):
        session = await client.new_session(
            schema.NewSessionRequest(cwd="/project", mcp_servers=[])
        )
        async with asyncio.TaskGroup() as tasks:
            prompt = tasks.create_task(
                request_model(
                    client.connection,
                    "session/prompt",
                    schema.PromptRequest(
                        session_id=session.response.session_id,
                        prompt=[acp.text_block("hello")],
                    ),
                    schema.PromptResponse,
                )
            )
            await callbacks.asked.wait()
            assert not prompt.done()
            assert callbacks.permissions[0].session_id == session.response.session_id
            assert len(callbacks.updates) == 1
            update = callbacks.updates[0].update
            assert isinstance(update, schema.AgentMessageChunk)
            assert isinstance(update.content, schema.TextContentBlock)
            assert len(update.content.text) == 100_000
            if cancel:
                await notify_model(
                    client.connection,
                    "session/cancel",
                    schema.CancelNotification(session_id=session.response.session_id),
                )
            else:
                callbacks.answered.set()
            response = await prompt
            assert response.stop_reason == ("cancelled" if cancel else "end_turn")


@pytest.mark.parametrize("cwd", ["relative", ""])
async def test_relative_workspace_is_rejected_before_session_creation(
    adapter_environment: dict[str, str], cwd: str
) -> None:
    async with NativeAcpClient.connect(
        AcpProvider.CLAUDE,
        Callbacks(),
        schema.InitializeRequest(protocol_version=1),
        env=adapter_environment,
    ) as client:
        with pytest.raises(ValueError, match="absolute"):
            await client.new_session(schema.NewSessionRequest(cwd=cwd, mcp_servers=[]))
        with pytest.raises(ValueError, match="absolute"):
            await client.new_session(
                schema.NewSessionRequest(
                    cwd="/project", additional_directories=[cwd], mcp_servers=[]
                )
            )
    log = await AsyncPath(adapter_environment["ACP_TEST_LOG"]).read_text()
    assert len(log.splitlines()) == 1


async def test_unsupported_negotiated_version_closes_process(
    adapter_environment: dict[str, str],
) -> None:
    with pytest.raises(ValueError, match="unsupported protocol 2"):
        async with NativeAcpClient.connect(
            AcpProvider.CODEX,
            Callbacks(),
            schema.InitializeRequest(protocol_version=1),
            env={**adapter_environment, "ACP_TEST_VERSION": "2"},
        ):
            pytest.fail("an incompatible connection must not be exposed")
    record = json.loads(
        await AsyncPath(adapter_environment["ACP_TEST_LOG"]).read_text()
    )
    with pytest.raises(ProcessLookupError):
        os.kill(record["pid"], 0)


async def test_peer_error_propagates_and_exception_closes_process(
    adapter_environment: dict[str, str],
) -> None:
    with pytest.raises(acp.RequestError, match="scripted refusal"):
        async with NativeAcpClient.connect(
            AcpProvider.CODEX,
            Callbacks(),
            schema.InitializeRequest(protocol_version=1),
            env=adapter_environment,
        ) as client:
            await client.connection.send_request("_fail", {})
    log = await AsyncPath(adapter_environment["ACP_TEST_LOG"]).read_text()
    record = json.loads(log.splitlines()[0])
    with pytest.raises(ProcessLookupError):
        os.kill(record["pid"], 0)


async def test_cancelling_connection_owner_reaps_process(
    adapter_environment: dict[str, str],
) -> None:
    connected = asyncio.Event()

    async def own_connection() -> None:
        async with NativeAcpClient.connect(
            AcpProvider.CLAUDE,
            Callbacks(),
            schema.InitializeRequest(protocol_version=1),
            env=adapter_environment,
        ):
            connected.set()
            await asyncio.Event().wait()

    async with asyncio.timeout(10), asyncio.TaskGroup() as tasks:
        owner = tasks.create_task(own_connection())
        await connected.wait()
        owner.cancel()
        with pytest.raises(asyncio.CancelledError):
            await owner
    record = json.loads(
        await AsyncPath(adapter_environment["ACP_TEST_LOG"]).read_text()
    )
    with pytest.raises(ProcessLookupError):
        os.kill(record["pid"], 0)


async def test_unsupported_requested_version_does_not_start_process(
    adapter_environment: dict[str, str],
) -> None:
    with pytest.raises(ValueError, match="requires protocol version 1"):
        async with NativeAcpClient.connect(
            AcpProvider.CLAUDE,
            Callbacks(),
            schema.InitializeRequest(protocol_version=2),
            env=adapter_environment,
        ):
            pytest.fail("an incompatible connection must not be opened")
    assert not await AsyncPath(adapter_environment["ACP_TEST_LOG"]).exists()
