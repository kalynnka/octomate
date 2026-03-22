"""napcat WebSocket tentacle — forward-WS connection to a napcat instance."""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import TYPE_CHECKING

import anyio
from pydantic import SecretStr
from websockets.asyncio.client import ClientConnection, connect
from websockets.exceptions import ConnectionClosed

from octomate.schemas.segments import AgentSegment, ImageSegment
from octomate.tentacles.base import PlatformMessage, SendTarget, Tentacle
from octomate.tentacles.feelers import NULL_FEELERS
from octomate.tentacles.napcat.chromo import NapcatChromo
from octomate.tentacles.napcat.ink import NapcatInk
from octomate.tentacles.napcat.schema import (
    SendGroupMsgAction,
    SendGroupMsgParams,
    SendPrivateMsgAction,
    SendPrivateMsgParams,
)
from octomate.utils import guess_image_ext
import base64
import uuid

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from octomate.schemas.events import MessageEvent
    from octomate.schemas.session import SessionKey

logger = logging.getLogger(__name__)


class NapcatTentacle(Tentacle):
    """Forward-WebSocket tentacle that connects *to* a napcat instance."""

    ws_url: str
    ws_client: ClientConnection | None
    ws_scope: anyio.CancelScope | None

    access_token: SecretStr | None
    backoff_base: float
    backoff_max: float
    backoff_factor: float

    ink: NapcatInk

    def __init__(
        self,
        tag: str,
        kick: Callable[[SessionKey, list[MessageEvent]], Awaitable[None]],
        *,
        ws_url: str,
        http_url: str,
        access_token: SecretStr | None = None,
        backoff_base: float = 1.0,
        backoff_max: float = 60.0,
        backoff_factor: float = 2.0,
        flush_delay: float = 0.5,
    ) -> None:
        self.ws_url = ws_url
        self.access_token = access_token
        self.ink = NapcatInk(http_url, access_token)
        self.chromo = NapcatChromo()
        self.feelers = NULL_FEELERS
        self.backoff_base = backoff_base
        self.backoff_max = backoff_max
        self.backoff_factor = backoff_factor
        self.ws_client = None
        self.ws_scope = None
        super().__init__(tag, kick, flush_delay=flush_delay)

    async def sense(self, ws: ClientConnection) -> None:
        async for raw in ws:
            await self.ingest(raw)

    async def activate(self) -> None:
        logger.info("Tentacle %s: connecting to %s", self.tag, self.ws_url)
        self.ws_scope = anyio.CancelScope()
        with self.ws_scope:
            delay = self.backoff_base
            while True:
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
                        logger.info("Tentacle %s: wired with Napcat", self.tag)
                        await self.sense(ws)

                except ConnectionClosed:
                    logger.warning(
                        "Tentacle %s: connection lost, reconnecting in %.1fs",
                        self.tag,
                        delay,
                    )
                except Exception as exc:
                    logger.error(
                        "Tentacle %s: connection failed (%s), retrying in %.1fs",
                        self.tag,
                        exc,
                        delay,
                    )
                finally:
                    self.ws_client = None

                await asyncio.sleep(delay)
                delay = min(delay * self.backoff_factor, self.backoff_max)

    async def deactivate(self) -> None:
        if self.ws_scope is not None:
            self.ws_scope.cancel()
        if self.ws_client is not None:
            try:
                await self.ws_client.close()
            except Exception:
                pass
            self.ws_client = None

    async def twitch(self, target: SendTarget, segments: list[AgentSegment]) -> None:
        if self.ws_client is None:
            logger.warning(
                "Tentacle %s: action cancelled, WebSocket not connected", self.tag
            )
            return
        await super().twitch(target, segments)

    async def send_platform_message(
        self,
        chat_id: str,
        chat_type: str,
        messages: list[PlatformMessage],
        reply_to: str | None = None,
    ) -> bool:
        if self.ws_client is None:
            return False
        for msg in messages:
            seg_data = json.loads(msg.content)
            if chat_type == "group":
                action = SendGroupMsgAction(
                    tentacle_id=self.tag,
                    params=SendGroupMsgParams(
                        group_id=chat_id, message=seg_data, reply=reply_to
                    ),
                )
            else:
                action = SendPrivateMsgAction(
                    tentacle_id=self.tag,
                    params=SendPrivateMsgParams(
                        user_id=chat_id, message=seg_data, reply=reply_to
                    ),
                )
            try:
                await self.ws_client.send(action.model_dump_json(exclude_none=True))
            except ConnectionClosed:
                logger.warning("Tentacle %s: WebSocket closed while sending", self.tag)
                return False
        return True

    async def absorb(self, seg: ImageSegment, save_dir: Path, message_id: str) -> None:
        try:
            url = seg.data.url
            if not url:
                url = await self.ink.get_image_url(str(seg.data.file))
            if not url:
                return

            resp = await self.ink.download(url)

            ext = guess_image_ext(resp.headers.get("content-type", ""), url)
            path = save_dir / f"{uuid.uuid4().hex}{ext}"
            await anyio.Path(path).write_bytes(resp.content)

            seg.data.file = str(path.resolve())
            seg.data.url = url
        except Exception:
            logger.warning(
                "Tentacle %s: failed to download image", self.tag, exc_info=True
            )

    async def secrete(self, seg: ImageSegment) -> None:
        try:
            apath = anyio.Path(seg.data.path)
            if await apath.exists():
                data = await apath.read_bytes()
                seg.data.file = f"base64://{base64.b64encode(data).decode()}"
        except Exception:
            logger.warning("Tentacle %s: failed to prepare outbound image", self.tag)
