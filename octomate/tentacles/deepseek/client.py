"""Authenticated HTTP Remote calls and multiplexed streams for dsh."""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from types import TracebackType
from uuid import uuid4

import httpx
from pydantic import HttpUrl, SecretStr, ValidationError
from websockets.asyncio.client import ClientConnection, connect

from octomate.tentacles.deepseek.wire import (
    ApprovalRequestedFrame,
    ClientRequest,
    ErrResult,
    MuxFrame,
    OkResult,
    QuestionRequestedFrame,
    RemoteCancellation,
    RemoteEnd,
    RemoteError,
    RemoteInvocation,
    RemoteItem,
    RemoteReady,
    RpcError,
    RpcReceipt,
    RpcResult,
    ServerResponse,
    SessionAssistantFrame,
    SessionEventFrame,
    SessionRecord,
    SessionSnapshot,
    StreamErrorFrame,
    remote_event_adapter,
    remote_message_adapter,
    session_follow_adapter,
)
from octomate.types.json import JsonObject, JsonValue

logger = logging.getLogger(__name__)

MUX_PATH = "/api/remote.mux"
EVENT_STREAM = "$events"

JSON_HEADERS = {"content-type": "application/json"}


@dataclass
class DeepseekApiClient:
    base_url: HttpUrl
    http_client: httpx.AsyncClient
    client_id: str | None = field(default=None, init=False)
    subscriptions: dict[str, asyncio.Future[None]] = field(
        default_factory=dict, init=False
    )

    async def __aenter__(self) -> DeepseekApiClient:
        # The one connection pool every call shares; opened here, closed in
        # __aexit__, so the client's lifetime is its owner's.
        await self.http_client.__aenter__()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None = None,
        exc_value: BaseException | None = None,
        traceback: TracebackType | None = None,
    ) -> None:
        await self.http_client.aclose()

    async def authenticate(self, token: SecretStr) -> None:
        """Exchange this harness process's launch token for its HTTP cookie."""
        try:
            response = await self.http_client.get(
                "/", params={"token": token.get_secret_value()}, follow_redirects=False
            )
        except httpx.HTTPError:
            raise RuntimeError(
                f"Could not authenticate to dsh at {self.base_url}"
            ) from None
        if response.status_code != 303 or not response.cookies:
            raise RuntimeError(
                f"dsh at {self.base_url} rejected the launch token "
                f"(HTTP {response.status_code})"
            )

    async def answering(self) -> bool:
        """Only a refused connection means no harness is listening."""
        result = await self.remote("settings/describe", {})
        if isinstance(result, OkResult):
            return True
        if result.error.code == "connection-refused":
            return False
        if result.error.code == "http-401":
            raise RuntimeError(
                f"dsh at {self.base_url} requires authentication after startup"
            )
        raise RuntimeError(
            f"dsh at {self.base_url} cannot serve the required Remote API "
            f"(settings/describe): {result.error.message}; update dsh and Octomate together"
        )

    async def call(self, method: str, payload: JsonValue) -> RpcResult:
        """One unary method: POST /api/<method>."""
        message = ClientRequest(rpc_id=str(uuid4()), method=method, payload=payload)
        try:
            response = await self.http_client.post(
                f"/api/{method}",
                content=message.model_dump_json(by_alias=True),
                headers=JSON_HEADERS,
            )
        except httpx.ConnectError as error:
            return ErrResult(
                error=RpcError(code="connection-refused", message=str(error))
            )
        except httpx.HTTPError as error:
            return ErrResult(error=RpcError(code="internal", message=str(error)))
        if not response.is_success:
            return ErrResult(
                error=RpcError(
                    code=f"http-{response.status_code}",
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
        """Remote argument keys are the upstream method's parameter names."""
        return await self.call(endpoint, {"args": args})

    async def respond(self, event_id: str, result: RpcResult | None) -> RpcReceipt:
        if self.client_id is None:
            raise RuntimeError("dsh Remote event stream is not ready")
        outcome: JsonObject
        if result is None:
            outcome = {"kind": "next"}
        elif isinstance(result, OkResult):
            outcome = {"kind": "result", "value": result.value}
        else:
            outcome = {
                "kind": "rejected",
                "error": {
                    "name": "Error",
                    "message": result.error.message,
                    "code": result.error.code,
                },
            }
        reply = await self.remote(
            "$events/result",
            {
                "clientId": self.client_id,
                "eventId": event_id,
                "outcome": outcome,
            },
        )
        return RpcReceipt(
            accepted=isinstance(reply, OkResult),
            reason=reply.error.message if isinstance(reply, ErrResult) else None,
        )

    async def open_mux(self) -> ClientConnection:
        request = self.http_client.build_request("GET", MUX_PATH)
        headers = (
            {"Cookie": request.headers["cookie"]}
            if "cookie" in request.headers
            else None
        )
        socket = await connect(self.ws_url(MUX_PATH), additional_headers=headers)
        try:
            await socket.send(
                json.dumps(
                    {
                        "type": "open",
                        "streamId": EVENT_STREAM,
                        "endpoint": "$events",
                        "payload": {"args": {}},
                    }
                )
            )
            raw = await asyncio.wait_for(socket.recv(), 10)
            message = remote_message_adapter.validate_json(raw)
            if not isinstance(message, RemoteItem) or message.stream_id != EVENT_STREAM:
                raise RuntimeError("dsh Remote event stream did not open")
            self.client_id = RemoteReady.model_validate(message.value).client_id
        except BaseException:
            await socket.close()
            raise
        return socket

    async def follow(self, socket: ClientConnection, session_id: str) -> None:
        ready: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        self.subscriptions[session_id] = ready
        try:
            await socket.send(
                json.dumps(
                    {
                        "type": "open",
                        "streamId": session_id,
                        "endpoint": "session/follow",
                        "payload": {
                            "args": {
                                "request": {
                                    "address": {
                                        "kind": "session",
                                        "sessionId": session_id,
                                    },
                                    "maxMessages": 1,
                                    "assistantStream": True,
                                }
                            }
                        },
                    }
                )
            )
            await asyncio.wait_for(ready, 10)
        except BaseException:
            await self.unfollow(socket, session_id)
            raise

    async def unfollow(self, socket: ClientConnection, session_id: str) -> None:
        self.subscriptions.pop(session_id, None)
        await socket.send(json.dumps({"type": "cancel", "streamId": session_id}))

    async def mux_frames(
        self, socket: ClientConnection
    ) -> AsyncIterator[tuple[str, MuxFrame]]:
        try:
            async for raw in socket:
                message = remote_message_adapter.validate_json(raw)
                session_id = message.stream_id
                if isinstance(message, RemoteError | RemoteEnd):
                    error = (
                        message.error
                        if isinstance(message, RemoteError)
                        else RpcError(
                            code="stream-ended", message="dsh Remote stream ended"
                        )
                    )
                    if session_id == EVENT_STREAM:
                        raise RuntimeError(error.message)
                    ready = self.subscriptions.get(session_id)
                    if ready is not None and not ready.done():
                        ready.set_exception(RuntimeError(error.message))
                    yield (
                        session_id,
                        StreamErrorFrame(
                            type="stream/error",
                            error=error,
                            session_id=session_id,
                        ),
                    )
                elif session_id == EVENT_STREAM:
                    frame = remote_event_adapter.validate_python(message.value)
                    if isinstance(frame, RemoteCancellation):
                        yield frame.event_id, frame
                    elif isinstance(frame, RemoteInvocation):
                        if frame.event == "approval/request":
                            yield (
                                frame.event_id,
                                ApprovalRequestedFrame.model_validate(
                                    {
                                        **frame.request,
                                        "type": "approval/requested",
                                        "sessionId": frame.agent_id,
                                        "approvalId": frame.event_id,
                                    }
                                ),
                            )
                        elif frame.event == "user-questions/request":
                            yield (
                                frame.event_id,
                                QuestionRequestedFrame.model_validate(
                                    {
                                        **frame.request,
                                        "type": "question/requested",
                                        "sessionId": frame.agent_id,
                                    }
                                ),
                            )
                        else:
                            receipt = await self.respond(frame.event_id, None)
                            if not receipt.accepted:
                                raise RuntimeError(
                                    f"dsh refused event delegation: {receipt.reason}"
                                )
                elif session_id in self.subscriptions:
                    entry = session_follow_adapter.validate_python(message.value)
                    if isinstance(entry, SessionSnapshot):
                        ready = self.subscriptions[session_id]
                        if not ready.done():
                            ready.set_result(None)
                    elif isinstance(entry, SessionRecord):
                        yield (
                            session_id,
                            SessionEventFrame(
                                type="session/event",
                                session_id=session_id,
                                event=entry.event,
                            ),
                        )
                    elif isinstance(entry, SessionAssistantFrame):
                        entry.session_id = session_id
                        yield session_id, entry
        finally:
            for ready in self.subscriptions.values():
                if not ready.done():
                    ready.set_exception(
                        RuntimeError("dsh Remote socket closed before session snapshot")
                    )

    def ws_url(self, path: str) -> str:
        scheme = {"http": "ws", "https": "wss"}[self.base_url.scheme]
        return f"{scheme}://{self.base_url.host}:{self.base_url.port}{path}"
