"""The inbound ACP channel and its connection ownership."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from fastapi import APIRouter
from pydantic import UUID7

from octomate.tentacles.base import Tentacle

if TYPE_CHECKING:
    from octomate.tentacles.acp.broker import AcpBroker


@dataclass
class AcpChannelTentacle(Tentacle[None, None]):
    """Expose the ACP API and track attached public sessions.

    The Octomate host mounts the router at connect time. Each broker dispatches
    kicks and reads the nerve for its WebSocket connection, so this API channel
    uses the base tentacle lifecycle rather than the IM channel's ink and siphon.
    """

    clients: dict[UUID7, AcpBroker] = field(
        default_factory=dict, init=False
    )  # Public threads attached to a connection.

    def routers(self) -> tuple[APIRouter, ...]:
        # The router's authentication dependency imports this class.
        from octomate.tentacles.acp.routes import build_acp_router

        return (build_acp_router(self),)
