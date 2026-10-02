"""The Ink execution boundary preserves native request and outcome semantics."""

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path
from unittest.mock import AsyncMock, Mock

import httpx
import pytest
from octomate_protocol.deepseek import ClientRequest, RpcError
from pydantic import HttpUrl, TypeAdapter, ValidationError
from pydantic_ai.exceptions import AgentRunError
from websockets.asyncio.client import ClientConnection

from octomate.tentacles.deepseek.client import DeepseekApiClient
from octomate.tentacles.deepseek.ink import DeepseekInk
from octomate.tentacles.deepseek.wire import (
    CommandError,
    CommandSuccess,
    MuxFrame,
    SessionEvent,
    SessionEventFrame,
    StreamErrorFrame,
)
from octomate.types.json import JsonObject, JsonValue

BASE_URL = "http://127.0.0.1:3080/"


async def test_subscriptions_capture_early_frames_and_isolate_sessions() -> None:
    client = AsyncMock(spec=DeepseekApiClient)
    socket = AsyncMock(spec=ClientConnection)
    client.open_mux.return_value = socket
    incoming: asyncio.Queue[tuple[str, MuxFrame] | None] = asyncio.Queue()
    invalidate = Mock()

    async def frames(socket: ClientConnection) -> AsyncIterator[tuple[str, MuxFrame]]:
        while (item := await incoming.get()) is not None:
            yield item
            incoming.task_done()

    async def follow(socket: ClientConnection, session_id: str) -> None:
        incoming.put_nowait(
            (
                session_id,
                SessionEventFrame(
                    type="session/event",
                    session_id=session_id,
                    event=SessionEvent(type="turn/start", seq=1, time=1),
                ),
            )
        )
        await asyncio.wait_for(incoming.join(), 1)

    client.mux_frames.side_effect = frames
    client.follow.side_effect = follow
    async with DeepseekInk(client) as ink:
        await ink.start(answer_interaction=AsyncMock(), invalidate_commands=invalidate)
        first = await ink.subscribe("first")
        second = await ink.subscribe("second")
        assert first.get_nowait().session_id == "first"
        assert second.get_nowait().session_id == "second"
        failure = StreamErrorFrame(
            type="stream/error",
            session_id="second",
            error=RpcError(code="internal", message="session failed"),
        )
        incoming.put_nowait(("second", failure))
        await asyncio.wait_for(incoming.join(), 1)
        assert first.empty()
        assert second.get_nowait() == failure
        await ink.unsubscribe("first")

        incoming.put_nowait(None)
        assert ink.mux_task is not None
        await asyncio.wait_for(ink.mux_task, 1)
        assert not ink.running
        assert first.empty()
        disconnected = second.get_nowait()
        assert isinstance(disconnected, StreamErrorFrame)
        assert disconnected.error.message == "dsh event stream closed"
        invalidate.assert_called_once_with()
        await ink.unsubscribe("second")
        assert not ink.subscribers

    assert ink.mux_task is None
    assert ink.mux_socket is None
    socket.close.assert_awaited_once()
    client.__aexit__.assert_awaited_once()


@pytest.fixture
def replies() -> JsonObject:
    payload = TypeAdapter(JsonObject).validate_json(
        (Path(__file__).parent / "fixtures/deepseek_command_results.json").read_text()
    )
    replies = payload["replies"]
    assert isinstance(replies, dict)
    return replies


@pytest.mark.parametrize(
    "case", ["success", "silent", "error", "unmatched", "remote_error"]
)
async def test_command_execution_preserves_arguments_and_native_outcomes(
    replies: JsonObject, case: str
) -> None:
    requests: list[ClientRequest] = []
    line = '/goal \t "raw argument"\n--flag=✓  '

    def respond(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/commands/execute"
        message = ClientRequest.model_validate_json(request.content)
        requests.append(message)
        assert message.payload == {
            "args": {
                "agentId": "session-1",
                "line": line,
                "submittedAttachments": [],
            }
        }
        return httpx.Response(
            200,
            json={
                "type": "server-response",
                "rpcId": message.rpc_id,
                "result": replies[case],
            },
        )

    client = DeepseekApiClient(
        HttpUrl(BASE_URL),
        httpx.AsyncClient(base_url=BASE_URL, transport=httpx.MockTransport(respond)),
    )
    async with DeepseekInk(client) as ink:
        if case == "remote_error":
            with pytest.raises(
                AgentRunError, match=r"Session is not loaded.*gateway/lookup-not-found"
            ):
                await ink.execute_command("session-1", line)
            assert len(requests) == 1
            return
        execution = await ink.execute_command("session-1", line)

    assert len(requests) == 1
    if case == "unmatched":
        assert execution is None
        return
    assert execution is not None
    if case == "error":
        assert execution.command_id == "command-3"
        assert isinstance(execution.result, CommandError)
        assert execution.result.text == "Unknown permission preset"
        return
    assert isinstance(execution.result, CommandSuccess)
    assert execution.command_id == ("command-1" if case == "success" else "command-2")
    assert execution.result.text == (
        "  Goal accepted.\n" if case == "success" else None
    )
    assert execution.result.source_event_seq == (17 if case == "success" else None)


@pytest.mark.parametrize("failure", ["connection-refused", "http-401"])
async def test_transport_failure_is_not_an_unmatched_or_command_error_result(
    failure: str,
) -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        if failure == "connection-refused":
            raise httpx.ConnectError("offline", request=request)
        return httpx.Response(401)

    async with DeepseekInk(
        DeepseekApiClient(
            HttpUrl(BASE_URL),
            httpx.AsyncClient(
                base_url=BASE_URL, transport=httpx.MockTransport(respond)
            ),
        )
    ) as ink:
        with pytest.raises(AgentRunError, match=failure):
            await ink.execute_command("session-1", "/permission workspace-write")


@pytest.mark.parametrize(
    "value",
    [
        {"commandId": "cmd-1"},
        {"commandId": "cmd-1", "result": {"kind": "unknown"}},
        {"commandId": "cmd-1", "result": {"kind": "error"}},
    ],
)
async def test_incomplete_or_unknown_outcomes_fail_validation(value: JsonValue) -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        message = ClientRequest.model_validate_json(request.content)
        return httpx.Response(
            200,
            json={
                "type": "server-response",
                "rpcId": message.rpc_id,
                "result": {"ok": True, "value": value},
            },
        )

    async with DeepseekInk(
        DeepseekApiClient(
            HttpUrl(BASE_URL),
            httpx.AsyncClient(
                base_url=BASE_URL, transport=httpx.MockTransport(respond)
            ),
        )
    ) as ink:
        with pytest.raises(ValidationError):
            await ink.execute_command("session-1", "/permission workspace-write")
