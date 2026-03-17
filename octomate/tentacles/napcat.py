"""napcat WebSocket tentacle — forward-WS connection to a napcat instance.

Connects to a napcat (NapNeko) OneBot 11 WebSocket endpoint, receives
events as JSON frames and pushes them through the Nerve, and sends
outbound actions over the same WebSocket.

Reference: https://napneko.github.io/onebot/
"""

from __future__ import annotations

import base64
import logging
import uuid
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any

import anyio
from pydantic import (
    Discriminator,
    SecretStr,
    Tag,
    TypeAdapter,
)
from websockets.asyncio.client import ClientConnection, connect
from websockets.exceptions import ConnectionClosed

from octomate.ink.napcat import NapcatInk, NapcatMask
from octomate.schemas.actions import ActionResponse
from octomate.schemas.events import Event, EventUnion, MessageEvent
from octomate.schemas.segments import AgentSegment, ImageSegment, TextSegment
from octomate.tentacles.base import SendTarget, Tentacle
from octomate.utils import guess_image_ext, strip_markdown

if TYPE_CHECKING:
    from octomate.octopus import Octopus

logger = logging.getLogger(__name__)


def inbound_discriminator(raw: Any) -> str:
    if isinstance(raw, dict) and "post_type" in raw:
        return "event"
    if isinstance(raw, Event):
        return "event"
    return "response"


InboundFrame = Annotated[
    Annotated[EventUnion, Tag("event")] | Annotated[ActionResponse, Tag("response")],
    Discriminator(inbound_discriminator),
]

inbound_adapter: TypeAdapter[InboundFrame] = TypeAdapter(InboundFrame)


class NapcatTentacle(Tentacle):
    """Forward-WebSocket tentacle that connects *to* a napcat instance."""

    ws_url: str
    ink: NapcatInk
    access_token: SecretStr | None
    backoff_base: float
    backoff_max: float
    backoff_factor: float
    _ws: ClientConnection | None
    _cancel_scope: anyio.CancelScope | None

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
        flush_delay: float = 0.5,
    ) -> None:
        self.ws_url = ws_url
        self.access_token = access_token
        self.ink = NapcatInk(http_url, access_token)
        self.backoff_base = backoff_base
        self.backoff_max = backoff_max
        self.backoff_factor = backoff_factor
        self._ws = None
        self._cancel_scope = None
        super().__init__(tag, octopus, flush_delay=flush_delay)

    def inspect(self) -> NapcatMask:
        mask = self.ink.inspect()
        logger.info("Tentacle %s: probed as %s (%s)", self.tag, mask.id, mask.name)
        return mask

    async def sense(self, ws: ClientConnection) -> None:
        async for raw in ws:
            try:
                frame = inbound_adapter.validate_json(raw)
            except Exception:
                logger.debug(
                    "Tentacle %s: unrecognised frame: %s",
                    self.tag,
                    raw[:200],
                )
                continue

            if isinstance(frame, ActionResponse):
                continue

            frame.tentacle_id = self.tag

            if isinstance(frame, MessageEvent):
                await self.submerge(frame)
                self.buffer.push(frame)

    async def activate(self) -> None:
        logger.info("Tentacle %s: connecting to %s", self.tag, self.ws_url)
        self._cancel_scope = anyio.CancelScope()
        with self._cancel_scope:
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
                        self._ws = ws
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
                    self._ws = None

                await anyio.sleep(delay)
                delay = min(delay * self.backoff_factor, self.backoff_max)

    async def deactivate(self) -> None:
        if self._cancel_scope is not None:
            self._cancel_scope.cancel()
        if self._ws is not None:
            try:
                await self._ws.close()
            except Exception:
                pass
            self._ws = None

    async def twitch(self, target: SendTarget, segments: list[AgentSegment]) -> None:
        if self._ws is None:
            logger.warning(
                "Tentacle %s: Action cancelled, WebSocket not connected", self.tag
            )
            return
        await super().twitch(target, segments)

    async def splash(self, target: SendTarget, segments: list[AgentSegment]) -> None:
        for seg in segments:
            if isinstance(seg, TextSegment):
                seg.data["text"] = strip_markdown(seg.data["text"])

        from octomate.schemas.actions import (
            SendGroupMsgAction,
            SendGroupMsgParams,
            SendPrivateMsgAction,
            SendPrivateMsgParams,
        )

        if target.chat_type == "group":
            msg = SendGroupMsgAction(
                tentacle_id=self.tag,
                params=SendGroupMsgParams(
                    group_id=target.chat_id,
                    message=segments,
                    reply=target.reply_to,
                ),
            )
        else:
            msg = SendPrivateMsgAction(
                tentacle_id=self.tag,
                params=SendPrivateMsgParams(
                    user_id=target.chat_id,
                    message=segments,
                    reply=target.reply_to,
                ),
            )
        frame = msg.model_dump_json(exclude_none=True)
        if self._ws:
            try:
                await self._ws.send(frame)
            except ConnectionClosed:
                logger.warning("Tentacle %s: WebSocket closed while sending", self.tag)

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
