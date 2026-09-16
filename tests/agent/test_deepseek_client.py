from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import AsyncIterator
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from pydantic import HttpUrl, SecretStr

from octomate.tentacles.deepseek.client import DeepseekApiClient
from octomate.tentacles.deepseek.wire import (
    ClientRequest,
    ErrResult,
    MuxFrame,
    OkResult,
    RpcError,
    RpcResult,
    SessionAssistantFrame,
)
from octomate.types.json import JsonObject

BASE_URL = "http://127.0.0.1:3080/"


async def test_cookie_exchange_authenticates_http_and_websocket_requests() -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            assert request.url.params["token"] == "launch-token"
            return httpx.Response(
                303,
                headers={
                    "Location": "/",
                    "Set-Cookie": "dsh-auth-test=signed-cookie; Path=/; HttpOnly",
                },
            )
        assert request.headers["cookie"] == "dsh-auth-test=signed-cookie"
        message = ClientRequest.model_validate_json(request.content)
        return httpx.Response(
            200,
            json={
                "type": "server-response",
                "rpcId": message.rpc_id,
                "result": {"ok": True, "value": {}},
            },
        )

    client = DeepseekApiClient(
        HttpUrl(BASE_URL),
        httpx.AsyncClient(base_url=BASE_URL, transport=httpx.MockTransport(respond)),
    )
    async with client:
        await client.authenticate(SecretStr("launch-token"))
        assert await client.answering()
        with patch(
            "octomate.tentacles.deepseek.client.connect", new_callable=AsyncMock
        ) as connect:
            connect.return_value.recv.return_value = '{"type":"item","streamId":"$events","value":{"type":"ready","clientId":"test-client"}}'
            await client.open_mux()
        connect.assert_awaited_once_with(
            "ws://127.0.0.1:3080/api/remote.mux",
            additional_headers={"Cookie": "dsh-auth-test=signed-cookie"},
        )


@pytest.mark.parametrize("status", [200, 303, 401])
async def test_authentication_requires_a_cookie_exchange(status: int) -> None:
    client = DeepseekApiClient(
        HttpUrl(BASE_URL),
        httpx.AsyncClient(
            base_url=BASE_URL,
            transport=httpx.MockTransport(lambda request: httpx.Response(status)),
        ),
    )
    async with client:
        with pytest.raises(RuntimeError, match="rejected the launch token"):
            await client.authenticate(SecretStr("private-token"))


async def test_authentication_transport_error_does_not_expose_the_token() -> None:
    def fail(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(str(request.url), request=request)

    client = DeepseekApiClient(
        HttpUrl(BASE_URL),
        httpx.AsyncClient(base_url=BASE_URL, transport=httpx.MockTransport(fail)),
    )
    async with client:
        with pytest.raises(RuntimeError) as caught:
            await client.authenticate(SecretStr("private-token"))

    assert "private-token" not in str(caught.value)
    assert caught.value.__suppress_context__


@pytest.mark.parametrize(
    ("status", "message"),
    [(401, "requires authentication"), (404, "cannot serve the required Remote API")],
)
async def test_a_reachable_unsupported_harness_is_not_treated_as_absent(
    status: int, message: str
) -> None:
    client = DeepseekApiClient(
        HttpUrl(BASE_URL),
        httpx.AsyncClient(
            base_url=BASE_URL,
            transport=httpx.MockTransport(lambda request: httpx.Response(status)),
        ),
    )
    async with client:
        with pytest.raises(RuntimeError, match=message):
            await client.answering()


@pytest.mark.parametrize(
    ("result", "outcome"),
    [
        (None, {"kind": "next"}),
        (OkResult(value="allowed-once"), {"kind": "result", "value": "allowed-once"}),
        (OkResult(value={"answers": []}), {"kind": "result", "value": {"answers": []}}),
        (
            ErrResult(error=RpcError(code="cancelled", message="No answer")),
            {
                "kind": "rejected",
                "error": {"name": "Error", "code": "cancelled", "message": "No answer"},
            },
        ),
    ],
)
async def test_remote_event_results_use_the_active_client_identity(
    result: RpcResult | None,
    outcome: JsonObject,
) -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        message = ClientRequest.model_validate_json(request.content)
        assert message.method == "$events/result"
        assert message.payload == {
            "args": {"clientId": "client-1", "eventId": "event-1", "outcome": outcome}
        }
        return httpx.Response(
            200,
            json={
                "type": "server-response",
                "rpcId": message.rpc_id,
                "result": {"ok": True},
            },
        )

    async with DeepseekApiClient(
        HttpUrl(BASE_URL),
        httpx.AsyncClient(base_url=BASE_URL, transport=httpx.MockTransport(respond)),
    ) as client:
        client.client_id = "client-1"
        assert (await client.respond("event-1", result)).accepted


async def test_session_stream_waits_for_snapshot_and_keeps_chunks_cursorless() -> None:
    incoming: asyncio.Queue[str] = asyncio.Queue()

    async def frames() -> AsyncIterator[str]:
        while True:
            yield await incoming.get()

    socket = AsyncMock()
    socket.__aiter__.side_effect = frames
    client = DeepseekApiClient(HttpUrl(BASE_URL), httpx.AsyncClient(base_url=BASE_URL))
    observed: list[MuxFrame] = []

    async def receive() -> None:
        async for _, frame in client.mux_frames(socket):
            observed.append(frame)

    pump = asyncio.create_task(receive())
    follow = asyncio.create_task(client.follow(socket, "session-1"))
    try:
        await asyncio.sleep(0)
        assert not follow.done()
        opened = json.loads(socket.send.call_args.args[0])
        assert opened["endpoint"] == "session/follow"
        assert opened["payload"]["args"]["request"]["assistantStream"] is True
        incoming.put_nowait(
            json.dumps(
                {
                    "type": "item",
                    "streamId": "session-1",
                    "value": {"type": "snapshot", "cursor": 99},
                }
            )
        )
        await asyncio.wait_for(follow, 1)
        incoming.put_nowait(
            json.dumps(
                {
                    "type": "item",
                    "streamId": "session-1",
                    "value": {
                        "type": "assistant-stream",
                        "frame": {
                            "type": "chunk",
                            "attemptId": "attempt",
                            "revision": 1,
                            "index": 0,
                            "time": 1,
                            "chunk": {"type": "text-delta", "text": "Hi"},
                        },
                    },
                }
            )
        )
        await asyncio.sleep(0)
        [chunk] = observed
        assert isinstance(chunk, SessionAssistantFrame)
        assert chunk.session_id == "session-1"
        assert "seq" not in chunk.model_dump_json()
        await client.unfollow(socket, "session-1")
        assert json.loads(socket.send.call_args.args[0]) == {
            "type": "cancel",
            "streamId": "session-1",
        }
    finally:
        pump.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await pump
        await client.http_client.aclose()


async def test_configured_token_does_not_prevent_starting_an_absent_harness() -> None:
    def refused(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("Connection refused")

    async with DeepseekApiClient(
        HttpUrl(BASE_URL),
        httpx.AsyncClient(base_url=BASE_URL, transport=httpx.MockTransport(refused)),
    ) as client:
        assert not await client.answering(SecretStr("stale-token"))
