"""The real SDK router over a scripted process's habitat stdio."""

import asyncio
import os
import sys
from dataclasses import replace
from functools import partial
from pathlib import Path
from typing import Literal
from unittest.mock import AsyncMock, Mock

import pytest
from httpx2 import URL
from logfire.testing import CaptureLogfire
from logfire.testing import capfire as capfire
from openai_codex import AsyncThread, CodexConfig
from openai_codex.api import ApprovalMode, Sandbox
from openai_codex.errors import CodexError, TransportClosedError
from openai_codex.generated.v2_all import (
    ReviewTarget,
    ThreadForkResponse,
    ThreadResumeResponse,
    ThreadStartResponse,
)
from opentelemetry import trace
from pydantic import BaseModel
from uuid_utils.compat import uuid7

from octomate.config.agents import CodexConfig as AgentCodexConfig
from octomate.config.habitat import SbxHabitatConfig
from octomate.habitat import HabitatError, ManagedHabitat
from octomate.habitat.sbx import SbxHabitat
from octomate.managers.habitat import HabitatManager
from octomate.nerve import Nerve
from octomate.schemas.habitat import InvocationScope
from octomate.tentacles.codex import pool as codex_pool
from octomate.tentacles.codex.client import SharedCodex
from octomate.tentacles.codex.ink import CodexInk
from octomate.types.json import JsonObject
from tests.habitat.test_contracts import IMAGE
from tests.support.managers import a_workspace_manager

pytestmark = pytest.mark.timeout(20)


class ProcessInfo(BaseModel):
    pid: int
    argv: list[str]
    environment: dict[str, str]
    trace: dict[str, str] | None = None


class EchoResponse(BaseModel):
    text: str


@pytest.mark.parametrize("method", ["thread/start", "thread/resume", "thread/fork"])
async def test_ink_reuses_and_isolates_habitat_clients(
    sandbox: SbxHabitat,
    config: CodexConfig,
    tmp_path: Path,
    method: Literal["thread/start", "thread/resume", "thread/fork"],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(codex_pool, "SharedCodex", SharedCodex)
    handler = AsyncMock(return_value={})
    ink = CodexInk(
        AgentCodexConfig(runtime=config),
        agent_id="codex",
        mcp_url=URL("http://127.0.0.1:9000/mcp"),
        handler=handler,
    )
    invalidated = asyncio.Event()
    ink.pool.watch_notifications = partial(
        ink.watch_runtime,
        invalidate_commands=Mock(side_effect=lambda _: invalidated.set()),
    )
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()
    other = replace(sandbox, user_id=uuid7())
    try:
        peers = await asyncio.gather(
            *(
                ink.pool.acquire(sandbox, env={"CODEX_HOME": str(first / ".codex")})
                for _ in range(4)
            )
        )
        assert all(runtime is peers[0] for runtime in peers)
        runtime = peers[0]
        sibling = await ink.pool.acquire(
            other, env={"CODEX_HOME": str(second / ".codex")}
        )
        assert sibling is not runtime
        assert runtime is not await ink.pool.acquire()
        info = await runtime._client.request("probe", {}, response_model=ProcessInfo)
        peer = await sibling._client.request("probe", {}, response_model=ProcessInfo)
        assert info.pid != peer.pid
        assert info.environment["CODEX_HOME"] == str(first / ".codex")
        assert peer.environment["CODEX_HOME"] == str(second / ".codex")
        assert info.environment["PWD"] == str(first)
        response = await runtime._client.request(
            method,
            {"threadId": str(uuid7())},
            response_model={
                "thread/start": ThreadStartResponse,
                "thread/resume": ThreadResumeResponse,
                "thread/fork": ThreadForkResponse,
            }[method],
        )
        thread_id = response.thread.id
        assert await ink.pool.acquire(thread_id=thread_id) is runtime
        with pytest.raises(ValueError, match="already open in another habitat"):
            await ink.pool.acquire(other, thread_id=thread_id)
        ink.thread_bindings[thread_id] = (uuid7(), None)
        assert await ink.pool.acquire(sandbox, thread_id=thread_id) is runtime
        core_thread = str(uuid7())
        ink.thread_bindings[core_thread] = (uuid7(), None)
        peer_thread = str(uuid7())
        ink.thread_bindings[peer_thread] = (uuid7(), None)
        assert await ink.pool.acquire(other, thread_id=peer_thread) is sibling
        with pytest.raises(ValueError, match="no thread in this habitat"):
            await sibling.transport.handler("approve", {"threadId": thread_id})
        handler.assert_not_called()
        await runtime.transport.handler("approve", {"threadId": thread_id})
        handler.assert_awaited_once_with("approve", {"threadId": thread_id})
        await sandbox.resources.aclose()
        await asyncio.wait_for(invalidated.wait(), 1)
        assert thread_id not in ink.thread_bindings
        assert peer_thread in ink.thread_bindings
        assert core_thread in ink.thread_bindings
        assert await ink.pool.acquire(thread_id=thread_id) is runtime
        assert not (await ink.pool.acquire()).transport.closing
        assert (
            await sibling._client.request("probe", {}, response_model=ProcessInfo)
        ).pid == peer.pid
        replacement = await ink.pool.acquire(
            sandbox, env={"CODEX_HOME": str(first / ".codex")}
        )
        assert replacement is not runtime
    finally:
        await ink.close()
        await sandbox.resources.aclose()
        await other.resources.aclose()


@pytest.fixture
def sandbox(tmp_path: Path) -> SbxHabitat:
    executable = tmp_path / "sbx"
    executable.write_text(
        f"#!{sys.executable}\n"
        + Path(__file__)
        .parents[2]
        .joinpath("agent/codex/fixtures/stdio_sbx.py")
        .read_text()
    )
    executable.chmod(0o700)
    return SbxHabitat(
        config=SbxHabitatConfig(executable=str(executable), image=IMAGE),
        user_id=uuid7(),
        root=tmp_path,
        config_dir=tmp_path / "config",
    )


@pytest.fixture
def config(tmp_path: Path) -> CodexConfig:
    return CodexConfig(
        codex_bin="/guest/codex",
        cwd=str(tmp_path),
        config_overrides=('model="test model"',),
        env={
            "CODEX_TEST_LOG": str(tmp_path / "process.json"),
            "CODEX_HOME": "/guest/config",
            "PWD": "must-be-replaced",
        },
    )


@pytest.mark.parametrize("instrument", [False, True])
@pytest.mark.parametrize("override", [False, True])
async def test_guest_router_keeps_reading_during_approval(
    sandbox: SbxHabitat,
    config: CodexConfig,
    monkeypatch: pytest.MonkeyPatch,
    instrument: bool,
    override: bool,
    capfire: CaptureLogfire,
) -> None:
    monkeypatch.setenv("CORE_ONLY", "must-not-reach-the-guest")
    if override:
        config = replace(config, launch_args_override=("custom-codex", "literal arg"))
    requested, release = asyncio.Event(), asyncio.Event()

    async def approve(method: str, params: JsonObject | None) -> JsonObject:
        assert method == "approve"
        requested.set()
        await release.wait()
        return {"decision": "accept"}

    span = trace.NonRecordingSpan(trace.SpanContext(1, 2, False, trace.TraceFlags(1)))
    async with SharedCodex(
        config, approve, instrument=instrument, habitat=sandbox
    ) as client:
        assert client.metadata.userAgent == "codex/0.171.0"
        with trace.use_span(span):
            first = await client._client.request(
                "probe",
                {"ask": True, "token": "must-not-be-traced"},
                response_model=ProcessInfo,
            )
        await requested.wait()
        second = await client._client.request("probe", {}, response_model=ProcessInfo)
        assert first.pid == second.pid
        assert (await client._client.next_notification()).method == "skills/changed"
        assert len(client.transport.requests) == 1
        release.set()
        assert (await client._client.next_notification()).method == "skills/changed"
        assert "guest diagnostic" in client.transport._stderr_tail()
    assert first.environment == {"PWD": config.cwd, "CODEX_HOME": "/guest/config"}
    expected = [
        "/guest/codex",
        "--config",
        'model="test model"',
        "app-server",
        "--listen",
        "stdio://",
    ]
    assert first.argv == (
        list(config.launch_args_override or ()) if override else expected
    )
    assert (first.trace is not None) is instrument
    if first.trace is not None:
        [rpc] = [
            span
            for span in capfire.exporter.exported_spans
            if span.name == "codex.rpc {method}"
            and span.parent is not None
            and span.parent.span_id == 2
        ]
        assert rpc.context is not None
        assert rpc.context.trace_id == 1
        assert first.trace["traceparent"] == (
            f"00-{rpc.context.trace_id:032x}-{rpc.context.span_id:016x}-01"
        )
        assert rpc.attributes is not None
        assert rpc.attributes["transport"] == "habitat-stdio"
    assert "must-not-be-traced" not in str(capfire.exporter.exported_spans_as_dict())
    assert client.transport.reader_thread is not None
    assert not client.transport.reader_thread.is_alive()
    with pytest.raises(ProcessLookupError):
        os.kill(first.pid, 0)


@pytest.mark.parametrize(
    ("mode", "message"),
    [
        ("exit", "guest diagnostic"),
        ("invalid", "Invalid Codex protocol"),
        ("partial", "Incomplete Codex protocol"),
    ],
)
async def test_failed_initialization_releases_the_guest(
    sandbox: SbxHabitat, config: CodexConfig, tmp_path: Path, mode: str, message: str
) -> None:
    config = replace(config, env={**(config.env or {}), "CODEX_TEST_MODE": mode})
    client = SharedCodex(config, AsyncMock(), instrument=False, habitat=sandbox)
    with pytest.raises(CodexError, match=message) as caught:
        async with client:
            pytest.fail("Initialization should fail")
    assert client.transport.token is None
    assert "private-payload" not in str(caught.value)
    info = ProcessInfo.model_validate_json((tmp_path / "process.json").read_text())
    with pytest.raises(ProcessLookupError):
        os.kill(info.pid, 0)


@pytest.mark.parametrize("during_initialization", [False, True])
async def test_cancellation_releases_reader_and_guest(
    sandbox: SbxHabitat,
    config: CodexConfig,
    tmp_path: Path,
    during_initialization: bool,
) -> None:
    if during_initialization:
        config = replace(config, env={**(config.env or {}), "CODEX_TEST_MODE": "hang"})
    entered = asyncio.Event()
    client = SharedCodex(config, AsyncMock(), instrument=False, habitat=sandbox)

    async def run() -> None:
        async with client:
            entered.set()
            await client._client.request("hang", {}, response_model=ProcessInfo)

    task = asyncio.create_task(run())
    if during_initialization:
        # The subprocess cannot signal an asyncio event in this test process.
        while not (tmp_path / "process.json").exists():  # noqa: ASYNC110
            await asyncio.sleep(0.01)
    else:
        await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert client.transport.token is None
    info = ProcessInfo.model_validate_json((tmp_path / "process.json").read_text())
    with pytest.raises(ProcessLookupError):
        os.kill(info.pid, 0)


async def test_closed_stream_fails_requests_without_hanging(
    sandbox: SbxHabitat, config: CodexConfig
) -> None:
    async with SharedCodex(
        config, AsyncMock(), instrument=False, habitat=sandbox
    ) as client:
        await client.close()
        assert client.transport.token is None
        with pytest.raises(TransportClosedError):
            await client._client.request("probe", {}, response_model=ProcessInfo)


async def test_large_rpc_round_trip_preserves_following_frames(
    sandbox: SbxHabitat, config: CodexConfig
) -> None:
    text = "large transcript\n" * 150_000
    async with SharedCodex(
        config, AsyncMock(), instrument=False, habitat=sandbox
    ) as client:
        response = await client._client.request(
            "echo", {"text": text}, response_model=EchoResponse
        )
        assert response.text == text
        following = await client._client.request(
            "probe", {}, response_model=ProcessInfo
        )
        assert following.pid > 0


@pytest.mark.parametrize("failed_request", [False, True])
async def test_context_exit_preserves_request_failure_but_reports_failed_close(
    sandbox: SbxHabitat, config: CodexConfig, failed_request: bool
) -> None:
    config = replace(
        config, env={**(config.env or {}), "CODEX_TEST_MODE": "close-failure"}
    )
    error = ValueError if failed_request else HabitatError
    message = "request failed" if failed_request else "exit 7"

    async def run() -> None:
        async with SharedCodex(config, AsyncMock(), instrument=False, habitat=sandbox):
            if failed_request:
                raise ValueError("request failed")

    with pytest.raises(error, match=message):
        await run()


async def test_invocations_reuse_clients_until_their_own_habitat_stops(
    sandbox: SbxHabitat, config: CodexConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager = HabitatManager(sandbox.config, Nerve(), a_workspace_manager())
    monkeypatch.setattr(SbxHabitat, "prepare", AsyncMock())
    owner = InvocationScope(user_id=uuid7(), conversation_id=uuid7())
    peer = owner.model_copy(update={"conversation_id": uuid7()})
    other = InvocationScope(user_id=uuid7(), conversation_id=uuid7())
    residence = manager.residence(owner)
    separate = manager.residence(other)
    client = SharedCodex(
        config, AsyncMock(), instrument=False, habitat=residence.habitat
    )
    other_client = SharedCodex(
        config, AsyncMock(), instrument=False, habitat=separate.habitat
    )
    resident = Mock(wraps=residence.habitat.resident)
    monkeypatch.setattr(residence.habitat, "resident", resident)
    stopped: list[str] = []

    async def stop(habitat: SbxHabitat) -> None:
        closing = client if habitat is residence.habitat else other_client
        assert closing.transport.closing
        assert closing.transport.reader_thread is not None
        assert not closing.transport.reader_thread.is_alive()
        stopped.append(habitat.id)

    monkeypatch.setattr(SbxHabitat, "stop", stop)
    try:
        async with manager.enter(owner, uuid7()):
            await asyncio.gather(*(client.start() for _ in range(4)))
            first = await client._client.request(
                "probe", {}, response_model=ProcessInfo
            )
        assert not client.transport.closing
        async with manager.enter(peer, uuid7()), manager.enter(other, uuid7()):
            await asyncio.gather(client.start(), other_client.start())
            replies = await asyncio.gather(
                *(
                    client._client.request("probe", {}, response_model=ProcessInfo)
                    for _ in range(4)
                )
            )
            separate_process = await other_client._client.request(
                "probe", {}, response_model=ProcessInfo
            )
            assert {reply.pid for reply in replies} == {first.pid}
            assert separate_process.pid != first.pid
            resident.assert_called_once()
        await manager.stop(residence)
        assert stopped == [residence.habitat.id]
        with pytest.raises(ProcessLookupError):
            os.kill(first.pid, 0)
        with pytest.raises(TransportClosedError, match="closed"):
            await client.start()
        still_running = await other_client._client.request(
            "probe", {}, response_model=ProcessInfo
        )
        assert still_running.pid == separate_process.pid
    finally:
        await manager.close()
    assert stopped == [residence.habitat.id, separate.habitat.id]
    with pytest.raises(ProcessLookupError):
        os.kill(separate_process.pid, 0)


async def test_cancelled_request_does_not_close_shared_client(
    sandbox: SbxHabitat, config: CodexConfig
) -> None:
    client = SharedCodex(config, AsyncMock(), instrument=False, habitat=sandbox)
    try:
        await client.start()
        first = await client._client.request("probe", {}, response_model=ProcessInfo)
        pending = asyncio.create_task(
            client._client.request("hang", {}, response_model=ProcessInfo)
        )
        assert (await client._client.next_notification()).method == "skills/changed"
        pending.cancel()
        with pytest.raises(asyncio.CancelledError):
            await pending
        second = await client._client.request("probe", {}, response_model=ProcessInfo)
        assert first.pid == second.pid
    finally:
        await sandbox.resources.aclose()
    with pytest.raises(ProcessLookupError):
        os.kill(first.pid, 0)


@pytest.mark.parametrize("os_sandbox", [True, False])
async def test_permission_updates_and_reviews_use_habitat_enforcement(
    os_sandbox: bool,
) -> None:
    handler = AsyncMock(return_value={})
    ink = CodexInk(
        AgentCodexConfig(),
        agent_id="codex",
        mcp_url=URL("http://core/mcp"),
        handler=handler,
    )
    client = SharedCodex(CodexConfig(), handler, instrument=False)
    client.habitat = Mock(spec=ManagedHabitat, os_sandbox=os_sandbox)
    client._client = AsyncMock()
    ink.pool.core_client = client
    conversation_id = uuid7()
    ink.thread_bindings["thread"] = (conversation_id, None)
    await ink.set_permission_mode(
        "thread",
        conversation_id=conversation_id,
        approval_mode=ApprovalMode.auto_review,
        sandbox=Sandbox.workspace_write,
    )
    ink.start_command = AsyncMock()
    await ink.start_turn(
        AsyncThread(client, "thread"),
        "review",
        approval_mode=ApprovalMode.auto_review,
        sandbox=Sandbox.workspace_write,
        cwd="/workspace",
        effort=None,
        model=None,
        output_schema=None,
        personality=None,
        summary=None,
        review=ReviewTarget.model_validate({"type": "uncommittedChanges"}),
    )
    assert client._client.request.await_count == 2
    for call in client._client.request.await_args_list:
        method, payload = call.args
        assert method == "thread/settings/update"
        assert payload["approvalPolicy"] == "on-request"
        assert payload["approvalsReviewer"] == "auto_review"
        if os_sandbox:
            assert payload["permissions"] == "octomate-workspace"
            assert "sandboxPolicy" not in payload
        else:
            assert "permissions" not in payload
            assert payload["sandboxPolicy"] == {
                "type": "externalSandbox",
                "networkAccess": "enabled",
            }
    client._client._start_turn.assert_not_awaited()
