"""The real SDK router over a scripted process's habitat stdio."""

import asyncio
import os
import sys
from dataclasses import replace
from pathlib import Path
from unittest.mock import AsyncMock, Mock

import pytest
from openai_codex import CodexConfig
from openai_codex.errors import CodexError, TransportClosedError
from opentelemetry import trace
from pydantic import BaseModel
from uuid_utils.compat import uuid7

from octomate.config.habitat import SbxHabitatConfig
from octomate.habitat import HabitatError
from octomate.habitat.sbx import SbxHabitat
from octomate.managers.habitat import HabitatManager
from octomate.nerve import Nerve
from octomate.schemas.habitat import InvocationScope
from octomate.tentacles.codex.client import SharedCodex
from octomate.types.json import JsonObject
from tests.support.managers import a_workspace_manager
from tests.test_habitat_contracts import IMAGE

pytestmark = pytest.mark.timeout(20)


class ProcessInfo(BaseModel):
    pid: int
    argv: list[str]
    environment: dict[str, str]
    trace: dict[str, str] | None = None


class EchoResponse(BaseModel):
    text: str


@pytest.fixture
def sandbox(tmp_path: Path) -> SbxHabitat:
    executable = tmp_path / "sbx"
    executable.write_text(
        f"#!{sys.executable}\n"
        + Path(__file__).with_name("fixtures").joinpath("stdio_sbx.py").read_text()
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
                "probe", {"ask": True}, response_model=ProcessInfo
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
        assert (
            first.trace["traceparent"]
            == "00-00000000000000000000000000000001-0000000000000002-01"
        )
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
    execute = Mock(wraps=residence.habitat.execute)
    monkeypatch.setattr(residence.habitat, "execute", execute)
    stopped: list[str] = []

    async def stop(habitat: SbxHabitat) -> None:
        closing = client if habitat is residence.habitat else other_client
        assert closing.transport.closing
        assert closing.transport.reader_thread is not None
        assert not closing.transport.reader_thread.is_alive()
        stopped.append(habitat.name)

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
            execute.assert_called_once()
        await manager.stop(residence)
        assert stopped == [residence.habitat.name]
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
    assert stopped == [residence.habitat.name, separate.habitat.name]
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
