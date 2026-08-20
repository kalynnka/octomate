"""The `/api` carrier: unary calls and `respond` over HTTP, events over one
downlink-only WebSocket.

dsh's own browser client is the reference. There is no SSE fallback to reach
for — the dsh web server answers a plain GET on the event paths with `426
upgrade required` — and the client sends no application data over the socket.
Business failures come back in the result's error branch exactly as dsh sent
them; only carrier failures (transport errors, non-2xx, an unparseable body)
are folded in locally, so callers never see a raise from the carrier.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass
from uuid import uuid4

import httpx
from pydantic import HttpUrl, ValidationError
from websockets.asyncio.client import ClientConnection, connect

from octomate.tentacles.agents.deepseek.wire import (
    ClientRequest,
    ClientResponse,
    ErrResult,
    MuxFrame,
    OkResult,
    RpcError,
    RpcReceipt,
    RpcResult,
    ServerRequest,
    ServerResponse,
    parse_mux_frame,
)
from octomate.types.json import JsonObject, JsonValue

logger = logging.getLogger(__name__)

MUX_PATH = "/api/events.mux"

JSON_HEADERS = {"content-type": "application/json"}


@dataclass
class DeepseekApiClient:
    base_url: HttpUrl
    http_client: httpx.AsyncClient

    async def __aenter__(self) -> DeepseekApiClient:
        # The one connection pool every call shares; opened here, closed in
        # __aexit__, so the client's lifetime is its owner's.
        await self.http_client.__aenter__()
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.http_client.aclose()

    async def answering(self) -> bool:
        """Whether a dsh serves `/api` at this client's base URL.
        `host.describe` is the probe dsh's own clients use; a carrier failure
        arrives as the error branch, so nothing answering is an answer, not a
        raise."""
        return isinstance(await self.call("host.describe", {}), OkResult)

    async def call(self, method: str, payload: JsonValue) -> RpcResult:
        """One unary method: POST /api/<method>."""
        message = ClientRequest(rpc_id=str(uuid4()), method=method, payload=payload)
        try:
            response = await self.http_client.post(
                f"/api/{method}",
                content=message.model_dump_json(by_alias=True),
                headers=JSON_HEADERS,
            )
        except httpx.HTTPError as error:
            return ErrResult(error=RpcError(code="internal", message=str(error)))
        if not response.is_success:
            return ErrResult(
                error=RpcError(
                    code="internal",
                    message=f"HTTP {response.status_code} from /api/{method}",
                )
            )
        try:
            return ServerResponse.model_validate_json(response.content).result
        except ValidationError as error:
            return ErrResult(
                error=RpcError(
                    code="internal",
                    message=f"unparseable /api/{method} response: {error}",
                )
            )

    async def remote(self, endpoint: str, args: JsonObject) -> RpcResult:
        """One method on dsh's *other* RPC plane, the typert remotes:
        `<namespace>/<method>` on the same POST transport, arguments wrapped in
        `args`. Undocumented — it is not in dsh's method map — but it is how
        dsh's own web client runs slash commands, and `/permission` is
        reachable only through here."""
        return await self.call(endpoint, {"args": args})

    async def respond(self, rpc_id: str, result: RpcResult) -> RpcReceipt:
        """Answer an answerable server-request by echoing its rpcId to
        POST /api/respond. The receipt belongs to the carrier: `not-pending`
        means the request was already settled elsewhere — a normal race."""
        message = ClientResponse(rpc_id=rpc_id, result=result)
        try:
            response = await self.http_client.post(
                "/api/respond",
                content=message.model_dump_json(by_alias=True),
                headers=JSON_HEADERS,
            )
        except httpx.HTTPError as error:
            return RpcReceipt(accepted=False, reason=str(error))
        if not response.is_success:
            return RpcReceipt(accepted=False, reason=f"HTTP {response.status_code}")
        try:
            return RpcReceipt.model_validate_json(response.content)
        except ValidationError as error:
            return RpcReceipt(accepted=False, reason=f"unparseable receipt: {error}")

    async def open_mux(self) -> ClientConnection:
        """Open the all-session mux socket. Split from the frame iterator so
        the readiness handshake can await the socket being open before anything
        prompts — the resync a connect triggers must not outrun the subscribed
        baseline."""
        return await connect(self.ws_url(MUX_PATH))

    @staticmethod
    async def mux_frames(
        socket: ClientConnection,
    ) -> AsyncIterator[tuple[str, MuxFrame]]:
        """The socket's frames as (rpcId, parsed frame) pairs. A message that
        is not even a typed frame is the carrier's problem: dropped with a
        debug line, and the stream stays alive. Ends when the socket closes."""
        async for raw in socket:
            if isinstance(raw, bytes):
                continue
            try:
                request = ServerRequest.model_validate_json(raw)
                frame = parse_mux_frame(request.payload)
            except ValidationError:
                logger.debug("dropping unparseable mux message: %.200s", raw)
                continue
            yield request.rpc_id, frame

    def ws_url(self, path: str) -> str:
        scheme = {"http": "ws", "https": "wss"}[self.base_url.scheme]
        return f"{scheme}://{self.base_url.host}:{self.base_url.port}{path}"
