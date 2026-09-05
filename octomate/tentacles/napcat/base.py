from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, ClassVar, Self

from pydantic import SecretStr
from rich.style import Style
from websockets.asyncio.client import ClientConnection, connect
from websockets.exceptions import ConnectionClosed

from octomate.config import NapcatChannelConfig
from octomate.tentacles.channel import ChannelSurfaces, ChannelTentacle
from octomate.tentacles.napcat.chromo import NapcatChromo
from octomate.tentacles.napcat.ink import NapcatInk
from octomate.tentacles.napcat.schema import NapcatOutboundMessage

if TYPE_CHECKING:
    from octomate.base import Octomate

logger = logging.getLogger(__name__)


class NapcatTentacle(ChannelTentacle[str | bytes, NapcatOutboundMessage]):
    brand_color: ClassVar[Style | None] = Style(color="#6A828B", bold=True)
    surfaces: ClassVar[ChannelSurfaces] = ChannelSurfaces(direct_message=True)

    ws_url: str
    ws_client: ClientConnection | None
    stop_event: asyncio.Event | None
    serve_task: asyncio.Task[None] | None

    access_token: SecretStr | None
    backoff_base: float
    backoff_max: float
    backoff_factor: float
    ink: NapcatInk
    chromo: NapcatChromo

    def __init__(
        self,
        id: str,
        octomate: Octomate,
        *,
        config: NapcatChannelConfig,
    ) -> None:
        ink = NapcatInk(config.http_url, config.access_token)
        chromo = NapcatChromo()
        super().__init__(
            id=id,
            octomate=octomate,
            ink=ink,
            chromo=chromo,
            config=config,
        )
        self.ink = ink
        self.chromo = chromo
        self.ws_url = config.ws_url
        self.access_token = config.access_token
        self.backoff_base = config.backoff_base
        self.backoff_max = config.backoff_max
        self.backoff_factor = config.backoff_factor
        self.ws_client = None
        self.stop_event = None
        self.serve_task = None

    async def __aenter__(self) -> Self:
        await super().__aenter__()
        # Napcat reads messages off the socket itself (`sense`), so the reconnect
        # loop must run as a background task the tentacle owns.
        self.serve_task = asyncio.create_task(self.serve(), name=f"napcat:{self.id}")
        return self

    async def __aexit__(self, *exc: object) -> None:
        if self.stop_event is not None:
            self.stop_event.set()
        if self.ws_client is not None:
            await self.ws_client.close()
            self.ws_client = None
        if self.serve_task is not None:
            self.serve_task.cancel()
            try:
                await self.serve_task
            except asyncio.CancelledError:
                pass
            self.serve_task = None
        await super().__aexit__(*exc)

    async def serve(self) -> None:
        logger.info("Channel %s: connecting to Napcat at %s", self.id, self.ws_url)
        self.stop_event = asyncio.Event()
        delay = self.backoff_base
        while self.stop_event is not None and not self.stop_event.is_set():
            try:
                extra_headers: dict[str, str] = {}
                if self.access_token:
                    extra_headers["Authorization"] = (
                        f"Bearer {self.access_token.get_secret_value()}"
                    )

                async with connect(
                    self.ws_url,
                    additional_headers=extra_headers or None,
                ) as ws:
                    self.ws_client = ws
                    delay = self.backoff_base
                    logger.info("Channel %s: connected to Napcat", self.id)
                    await self.sense(ws)
            except ConnectionClosed:
                logger.warning(
                    "Channel %s: Napcat connection lost, reconnecting in %.1fs",
                    self.id,
                    delay,
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.error(
                    "Channel %s: Napcat connection failed (%s), retrying in %.1fs",
                    self.id,
                    exc,
                    delay,
                )
            finally:
                self.ws_client = None

            if self.stop_event is None or self.stop_event.is_set():
                break
            try:
                await asyncio.wait_for(self.stop_event.wait(), timeout=delay)
            except TimeoutError:
                pass
            delay = min(delay * self.backoff_factor, self.backoff_max)

    async def sense(self, ws: ClientConnection) -> None:
        async for raw in ws:
            await self.ingest(raw)
