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
from functools import cached_property
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any

import anyio
import httpx
from pydantic import BaseModel, Discriminator, SecretStr, Tag, TypeAdapter
from websockets.asyncio.client import ClientConnection, connect
from websockets.exceptions import ConnectionClosed

from octomate.schemas.actions import ActionResponse
from octomate.schemas.events import Event, EventUnion, MessageEvent
from octomate.schemas.segments import AgentSegment, ImageSegment
from octomate.tentacles.base import Mask, SendTarget, Tentacle
from octomate.utils import guess_image_ext

if TYPE_CHECKING:
    from octomate.nerve import OctopusNerve

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


class AccountInfo(BaseModel):
    user_id: int
    nickname: str
    uid: str = ""
    qid: str = ""
    uin: str = ""
    nick: str = ""
    long_nick: str = ""
    sex: str = "unknown"
    age: int = 0
    qq_level: int = 0
    login_days: int = 0
    reg_time: int = 0
    is_vip: bool = False
    is_years_vip: bool = False
    vip_level: int = 0


class NapcatTentacle(Tentacle):
    """Forward-WebSocket tentacle that connects *to* a napcat instance."""

    ws_url: str
    http_url: str
    access_token: SecretStr | None
    backoff_base: float
    backoff_max: float
    backoff_factor: float
    profile: AccountInfo | None
    _ws: ClientConnection | None
    _cancel_scope: anyio.CancelScope | None

    def __init__(
        self,
        tag: str,
        nerve: OctopusNerve,
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
        self.http_url = http_url
        self.access_token = access_token
        self.backoff_base = backoff_base
        self.backoff_max = backoff_max
        self.backoff_factor = backoff_factor
        self.profile = None
        self._ws = None
        self._cancel_scope = None
        super().__init__(tag, nerve, flush_delay=flush_delay)

    @cached_property
    def ink(self) -> httpx.AsyncClient:
        headers: dict[str, str] = {}
        if self.access_token:
            headers["Authorization"] = f"Bearer {self.access_token.get_secret_value()}"
        return httpx.AsyncClient(base_url=self.http_url, headers=headers)

    def inspect(self) -> Mask:
        headers: dict[str, str] = {}
        if self.access_token:
            headers["Authorization"] = f"Bearer {self.access_token.get_secret_value()}"
        try:
            resp = httpx.post(
                f"{self.http_url}/get_login_info", json={}, headers=headers
            )
            resp.raise_for_status()
            login_data = resp.json().get("data")

            if not login_data or "user_id" not in login_data:
                return Mask(id="", name="")

            resp = httpx.post(
                f"{self.http_url}/get_stranger_info",
                json={"user_id": login_data["user_id"]},
                headers=headers,
            )
            resp.raise_for_status()
            profile_data = resp.json().get("data")
            if profile_data:
                self.profile = AccountInfo.model_validate(profile_data)

            mask = Mask(
                id=str(login_data["user_id"]),
                name=login_data.get("nickname", ""),
            )
            logger.info("Tentacle %s: probed as %s (%s)", self.tag, mask.id, mask.name)
            return mask
        except Exception:
            logger.warning("Tentacle %s: probe failed, identity unknown", self.tag)
            return Mask(id="", name="")

    async def deactivate(self) -> None:
        if self._cancel_scope is not None:
            self._cancel_scope.cancel()
        if self._ws is not None:
            try:
                await self._ws.close()
            except Exception:
                pass
            self._ws = None

    async def twitch(self, action: Any) -> None:
        if self._ws is None:
            logger.warning(
                "Tentacle %s: cannot send, WebSocket not connected", self.tag
            )
            return
        await super().twitch(action)

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
                        logger.info("Tentacle %s: connected", self.tag)

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
                                self.sense(frame)

                except ConnectionClosed:
                    logger.warning(
                        "Tentacle %s: connection lost, reconnecting in %.1fs",
                        self.tag,
                        delay,
                    )
                except OSError as exc:
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

    async def splash(self, target: SendTarget, segments: list[AgentSegment]) -> None:
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

    async def jet(self, action: Any) -> None:
        frame = action.model_dump_json(exclude_none=True)
        if self._ws:
            try:
                await self._ws.send(frame)
            except ConnectionClosed:
                logger.warning("Tentacle %s: WebSocket closed while sending", self.tag)

    async def absorb(self, seg: ImageSegment, save_dir: Path) -> None:
        try:
            url = seg.data.url
            if not url:
                resp = await self.ink.post(
                    "/get_image", json={"file": str(seg.data.file)}
                )
                resp.raise_for_status()
                url = resp.json().get("data", {}).get("url")
            if not url:
                return

            resp = await self.ink.get(url)
            resp.raise_for_status()

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
