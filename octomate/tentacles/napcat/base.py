"""napcat WebSocket tentacle — forward-WS connection to a napcat instance."""

from __future__ import annotations

import asyncio
import base64
import json
import logging
from pathlib import Path
from typing import TYPE_CHECKING

import anyio
from pydantic import SecretStr
from uuid_utils import uuid7
from websockets.asyncio.client import ClientConnection, connect
from websockets.exceptions import ConnectionClosed

from octomate.schemas.segments import AgentSegment, ImageSegment
from octomate.schemas.session import SessionKey
from octomate.tentacles.base import ChannelTentacle, PlatformMessage
from octomate.tentacles.napcat.chromo import NapcatChromo
from octomate.tentacles.napcat.ink import NapcatInk
from octomate.tentacles.napcat.schema import (
    SendGroupMsgAction,
    SendGroupMsgParams,
    SendPrivateMsgAction,
    SendPrivateMsgParams,
)
from octomate.utils import guess_image_ext

if TYPE_CHECKING:
    from octomate.agents.pulse import PulseAgents
    from octomate.memory.base import OctopusMemory
    from octomate.octopus import Octopus

logger = logging.getLogger(__name__)


class NapcatTentacle(ChannelTentacle):
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
        octopus: Octopus,
        *,
        ws_url: str,
        http_url: str,
        access_token: SecretStr | None = None,
        backoff_base: float = 1.0,
        backoff_max: float = 60.0,
        backoff_factor: float = 2.0,
        agents: PulseAgents,
        memory: OctopusMemory,
        flush_delay: float = 0.5,
    ) -> None:
        self.ws_url = ws_url
        self.access_token = access_token
        self.ink = NapcatInk(http_url, access_token)
        self.chromo = NapcatChromo()
        self.backoff_base = backoff_base
        self.backoff_max = backoff_max
        self.backoff_factor = backoff_factor
        self.ws_client = None
        self.ws_scope = None
        super().__init__(tag, octopus, agents, memory, flush_delay=flush_delay)

    async def sense(self, ws: ClientConnection) -> None:
        async for raw in ws:
            await self.ingest(raw)

    async def activate(self) -> None:
        logger.info("Tentacle %s: connecting to %s", self.id, self.ws_url)
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
                        logger.info("Tentacle %s: wired with Napcat", self.id)
                        await self.sense(ws)

                except ConnectionClosed:
                    logger.warning(
                        "Tentacle %s: connection lost, reconnecting in %.1fs",
                        self.id,
                        delay,
                    )
                except Exception as exc:
                    logger.error(
                        "Tentacle %s: connection failed (%s), retrying in %.1fs",
                        self.id,
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

    async def twitch(self, key: SessionKey, segments: list[AgentSegment]) -> None:
        if self.ws_client is None:
            logger.warning(
                "Tentacle %s: action cancelled, WebSocket not connected", self.id
            )
            return
        await super().twitch(key, segments)

    async def send_platform_message(
        self,
        chat_id: str,
        chat_type: str,
        messages: list[PlatformMessage],
        reply_to: str | None = None,
        reply_in_thread: bool = False,
    ) -> str | None:
        if self.ws_client is None:
            return None
        for msg in messages:
            seg_data = json.loads(msg.content)
            if chat_type == "group":
                action = SendGroupMsgAction(
                    tentacle_id=self.id,
                    params=SendGroupMsgParams(
                        group_id=chat_id, message=seg_data, reply=reply_to
                    ),
                )
            else:
                action = SendPrivateMsgAction(
                    tentacle_id=self.id,
                    params=SendPrivateMsgParams(
                        user_id=chat_id, message=seg_data, reply=reply_to
                    ),
                )
            try:
                await self.ws_client.send(action.model_dump_json(exclude_none=True))
            except ConnectionClosed:
                logger.warning("Tentacle %s: WebSocket closed while sending", self.id)
                return None
        return None

    async def absorb(self, seg: ImageSegment, save_dir: Path, message_id: str) -> None:
        try:
            url = seg.data.url
            if not url:
                url = await self.ink.get_image_url(str(seg.data.file))
            if not url:
                return

            resp = await self.ink.download(url)

            ext = guess_image_ext(resp.headers.get("content-type", ""), url)
            path = save_dir / f"{uuid7().hex}{ext}"
            await anyio.Path(path).write_bytes(resp.content)

            seg.data.file = str(path.resolve())
            seg.data.url = url
        except Exception:
            logger.warning(
                "Tentacle %s: failed to download image", self.id, exc_info=True
            )

    async def secrete(self, seg: ImageSegment) -> None:
        try:
            apath = anyio.Path(seg.data.path)
            if await apath.exists():
                data = await apath.read_bytes()
                seg.data.file = f"base64://{base64.b64encode(data).decode()}"
        except Exception:
            logger.warning("Tentacle %s: failed to prepare outbound image", self.id)
