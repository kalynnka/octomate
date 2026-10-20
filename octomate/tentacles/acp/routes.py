"""ACP v1 at `WS /api/acp`, exposing Octomate as one agent.

The Authorization header carries an `acp`-scoped bearer. Send one JSON-RPC object
per text frame; this is a custom WebSocket transport, not Streamable HTTP.
Initialize, create or reopen a session, and select a returned model option using
`session/set_config_option` with `configId: "model"`. Provider names never occur
in the URL. See `AcpBroker` for session ownership, workspace and MCP contracts.
"""

from typing import Annotated

from anyio import CancelScope
from fastapi import APIRouter, Depends, WebSocket
from pydantic import JsonValue

from octomate.schemas.user import UserProfile
from octomate.tentacles.acp.auth import acp_sender
from octomate.tentacles.acp.broker import AcpBroker
from octomate.tentacles.acp.channel import AcpChannelTentacle
from octomate.tentacles.acp.transport import websocket_connection


def build_acp_router(tentacle: AcpChannelTentacle) -> APIRouter:
    """Bind authentication and connections to the registered ACP instance."""
    router = APIRouter(prefix="/api/acp", tags=["acp"])

    @router.websocket("")
    async def acp_connection(
        websocket: WebSocket,
        sender: Annotated[UserProfile, Depends(acp_sender(tentacle))],
    ) -> None:
        """Serve the authenticated caller through registered agents and the reflex."""

        async def handle(
            method: str, params: JsonValue | None, notification: bool
        ) -> JsonValue | None:
            return await broker.handle(method, params, notification)

        await websocket.accept()
        async with websocket_connection(websocket, handle) as connection:
            broker = AcpBroker(tentacle, sender, connection)

            async def shutdown() -> None:
                broker.disconnected = True
                with CancelScope(shield=True):
                    try:
                        await connection.close()
                    finally:
                        await broker.close()

            try:
                await connection.main_loop()
            finally:
                await shutdown()

    return router
