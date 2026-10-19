"""Native ACP v1 at `WS /api/acp/{provider}`, where provider is claude or codex.

The Authorization header must carry an `acp`-scoped bearer. Each WebSocket owns
one adapter process and the sessions created through it. One JSON-RPC object is
sent per text frame; this is Octomate's custom ACP transport, not Streamable HTTP.

Initialize first, then use `session/new`, `session/prompt` and `session/cancel`.
Client capabilities and agent callbacks cross the connection directly, including
permission requests, filesystem access and terminals. No request/reply traffic
uses the nerve. Session loading, listing and configuration are not exposed.

The adapter must already be installed and logged in as the service account.
Provider authentication is not exposed through ACP. Workspace paths and stdio MCP
commands run on the adapter's host; forwarding client capabilities does not mount
the caller's directory there. The broker registers native identities, but this
route does not ingest transcripts or project stream events into the ledger.

Disconnecting closes the adapter and loses this connection's drive authority.
Provider sessions and Octomate's registered identities are not deleted. API-key
expiry and revocation apply to the next connection attempt.
"""

from contextlib import AsyncExitStack
from typing import Annotated

import acp
from acp import schema
from anyio import CancelScope
from fastapi import APIRouter, Depends, WebSocket
from pydantic import JsonValue

from octomate.base import Octomate
from octomate.dependencies import application
from octomate.schemas.user import UserProfile
from octomate.tentacles.acp.adapter import json_value_adapter
from octomate.tentacles.acp.auth import acp_sender
from octomate.tentacles.acp.broker import NativeAcpBroker
from octomate.tentacles.acp.client import AcpProvider, NativeAcpClient
from octomate.tentacles.acp.transport import websocket_connection

acp_router = APIRouter(prefix="/api/acp", tags=["acp"])


@acp_router.websocket("/{provider}")
async def native_acp(
    websocket: WebSocket,
    provider: AcpProvider,
    sender: Annotated[UserProfile, Depends(acp_sender)],
    app: Annotated[Octomate, Depends(application)],
) -> None:
    """Serve one authenticated caller; only initialize starts an adapter process."""
    broker: NativeAcpBroker | None = None
    initializing = False
    adapters = AsyncExitStack()

    async def callback(
        method: str, params: JsonValue | None, notification: bool
    ) -> JsonValue | None:
        if notification:
            await connection.send_notification(method, params)
            return None
        return await connection.send_request(method, params)

    async def handle(
        method: str, params: JsonValue | None, notification: bool
    ) -> JsonValue | None:
        nonlocal broker, initializing
        if notification:
            if method == "session/cancel" and broker is not None:
                await broker.cancel(schema.CancelNotification.model_validate(params))
            return None
        if method == "initialize":
            if initializing or broker is not None:
                raise acp.RequestError.invalid_request(
                    {"reason": "Already initialized"}
                )
            request = schema.InitializeRequest.model_validate(params)
            request.protocol_version = acp.PROTOCOL_VERSION
            initializing = True
            try:
                client = await adapters.enter_async_context(
                    NativeAcpClient.connect(provider, callback, request)
                )
                broker = NativeAcpBroker(
                    client, sender, app.threads, app.conversations, app.projects
                )
            finally:
                initializing = False
            capabilities = client.initialization.agent_capabilities
            response = client.initialization.model_copy(
                update={
                    "auth_methods": [],
                    "agent_capabilities": schema.AgentCapabilities(
                        auth=None,
                        prompt_capabilities=(
                            capabilities.prompt_capabilities if capabilities else None
                        ),
                        mcp_capabilities=(
                            capabilities.mcp_capabilities if capabilities else None
                        ),
                    ),
                }
            )
        elif broker is None:
            raise acp.RequestError.invalid_request({"reason": "Initialize first"})
        elif method == "session/new":
            try:
                response = await broker.new_session(
                    schema.NewSessionRequest.model_validate(params)
                )
            except ValueError as error:
                raise acp.RequestError.invalid_params({"reason": str(error)}) from error
            response = response.model_copy(
                update={"modes": None, "config_options": None}
            )
        elif method == "session/prompt":
            response = await broker.prompt(schema.PromptRequest.model_validate(params))
        else:
            raise acp.RequestError.method_not_found(method)
        return json_value_adapter.validate_python(
            response.model_dump(mode="json", by_alias=True, exclude_none=True)
        )

    await websocket.accept()
    async with websocket_connection(websocket, handle) as connection:
        try:
            await connection.main_loop()
        finally:
            with CancelScope(shield=True):
                await adapters.aclose()
