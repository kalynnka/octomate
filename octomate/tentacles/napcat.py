"""napcat WebSocket tentacle — forward-WS connection to a napcat instance.

Connects to a napcat (NapNeko) OneBot 11 WebSocket endpoint, receives
events as JSON frames and pushes them through the Nerve, and sends
outbound actions over the same WebSocket.

Reference: https://napneko.github.io/onebot/
"""

from __future__ import annotations

import logging

import anyio
from websockets.asyncio.client import ClientConnection, connect
from websockets.exceptions import ConnectionClosed

from octomate.config import NapcatTentacleConfig
from octomate.nerve import ActionUnion
from octomate.schemas.adaptors import inbound_adapter
from octomate.schemas.events import ActionResponse
from octomate.tentacles.base import BaseTentacle

logger = logging.getLogger(__name__)


class NapcatTentacle(BaseTentacle):
    """Forward-WebSocket tentacle that connects *to* a napcat instance."""

    def __init__(self, config: NapcatTentacleConfig) -> None:
        super().__init__(config.name)
        self.config = config
        self._ws: ClientConnection | None = None
        self._cancel_scope: anyio.CancelScope | None = None

    async def start(self) -> None:
        """Connect to napcat and start the receive loop.

        This method is designed to be launched inside an
        ``anyio.create_task_group`` so the receive loop runs as a
        background task.
        """
        logger.info("Tentacle %s: connecting to %s", self.name, self.config.ws_url)
        self._cancel_scope = anyio.CancelScope()
        with self._cancel_scope:
            await self._connect_loop()

    async def stop(self) -> None:
        """Cancel the receive loop and close the WebSocket."""
        if self._cancel_scope is not None:
            self._cancel_scope.cancel()
        await self._close_ws()

    async def send(self, action: ActionUnion) -> None:
        """Serialize an action model to JSON and write it to the WebSocket."""
        if self._ws is None:
            logger.warning(
                "Tentacle %s: cannot send, WebSocket not connected", self.name
            )
            return

        frame = action.model_dump_json(exclude_none=True)
        try:
            await self._ws.send(frame)
        except ConnectionClosed:
            logger.warning(
                "Tentacle %s: WebSocket closed while sending", self.name
            )

    async def _connect_loop(self) -> None:
        """Connect with exponential back-off, delegating to _receive_loop."""
        delay = self.config.backoff_base
        while True:
            try:
                extra_headers: dict[str, str] = {}
                if self.config.access_token:
                    extra_headers["Authorization"] = f"Bearer {self.config.access_token}"

                async with connect(
                    self.config.ws_url,
                    additional_headers=extra_headers or None,
                ) as ws:
                    self._ws = ws
                    delay = self.config.backoff_base
                    logger.info("Tentacle %s: connected", self.name)
                    await self._receive_loop(ws)
            except ConnectionClosed:
                logger.warning(
                    "Tentacle %s: connection lost, reconnecting in %.1fs",
                    self.name,
                    delay,
                )
            except OSError as exc:
                logger.error(
                    "Tentacle %s: connection failed (%s), retrying in %.1fs",
                    self.name,
                    exc,
                    delay,
                )
            finally:
                self._ws = None

            await anyio.sleep(delay)
            delay = min(delay * self.config.backoff_factor, self.config.backoff_max)

    async def _receive_loop(self, ws: ClientConnection) -> None:
        """Read JSON frames, parse into events, and push to the Nerve."""
        async for raw in ws:
            try:
                frame = inbound_adapter.validate_json(raw)
            except Exception:
                logger.debug(
                    "Tentacle %s: unrecognised frame: %s",
                    self.name,
                    raw[:200],
                )
                continue

            if isinstance(frame, ActionResponse):
                logger.debug(
                    "Tentacle %s: received action response (echo=%s)",
                    self.name,
                    frame.echo,
                )
                continue

            await self._push_event(frame)

    async def _close_ws(self) -> None:
        if self._ws is not None:
            try:
                await self._ws.close()
            except Exception:  # noqa: BLE001
                pass
            self._ws = None
