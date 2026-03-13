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

import anyio
import httpx
from pydantic import BaseModel
from websockets.asyncio.client import ClientConnection, connect
from websockets.exceptions import ConnectionClosed

from octomate.config import NapcatTentacleConfig
from octomate.schemas.actions import (
    ActionResponse,
    SendGroupMsgAction,
    SendPrivateMsgAction,
)
from octomate.schemas.adaptors import ActionUnion, inbound_adapter
from octomate.schemas.events import GroupMessageEvent, MessageEvent
from octomate.schemas.segments import AgentSegment, ImageSegment
from octomate.tentacles.base import BaseTentacle
from octomate.utils import guess_image_ext

logger = logging.getLogger(__name__)

FILES_ROOT = Path(".octomate/files")


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


class NapcatTentacle(BaseTentacle):
    """Forward-WebSocket tentacle that connects *to* a napcat instance."""

    config: NapcatTentacleConfig
    profile: AccountInfo | None
    http_client: httpx.AsyncClient

    _ws: ClientConnection | None
    _cancel_scope: anyio.CancelScope | None

    def __init__(self, config: NapcatTentacleConfig) -> None:
        super().__init__(config)
        self.config = config
        self.profile: AccountInfo | None = None

        self._ws: ClientConnection | None = None
        self._cancel_scope: anyio.CancelScope | None = None

        headers = {}
        if config.access_token:
            headers["Authorization"] = (
                f"Bearer {config.access_token.get_secret_value()}"
            )

        self.http_client = httpx.AsyncClient(
            base_url=str(config.http_url),
            headers=headers,
        )

    async def introspect(self) -> None:
        try:
            # TODO: wrap and make napcat API tools for this tentacle's agent to use
            resp = await self.http_client.post("/get_login_info", json={})
            resp.raise_for_status()
            login_data = resp.json().get("data")

            if not login_data or "user_id" not in login_data:
                return

            self.self_id = login_data["user_id"]
            self.self_name = login_data.get("nickname")

            resp = await self.http_client.post(
                "/get_stranger_info", json={"user_id": self.self_id}
            )
            resp.raise_for_status()
            profile_data = resp.json().get("data")

            if profile_data:
                self.profile = AccountInfo.model_validate(profile_data)

            logger.info(
                "Tentacle %s: introspected as %s (%s)",
                self.name,
                self.self_id,
                self.self_name,
            )
        except Exception:
            logger.warning(
                "Tentacle %s: introspection failed, identity unknown",
                self.name,
            )

    async def activate(self) -> None:
        logger.info("Tentacle %s: connecting to %s", self.name, self.config.ws_url)
        await self.introspect()
        self._cancel_scope = anyio.CancelScope()
        with self._cancel_scope:
            await self._connect_loop()

    async def deactivate(self) -> None:
        """Cancel the receive loop and close the WebSocket."""
        if self._cancel_scope is not None:
            self._cancel_scope.cancel()
        if self._ws is not None:
            try:
                await self._ws.close()
            except Exception:  # noqa: BLE001
                pass
            self._ws = None

    async def act(self, action: ActionUnion) -> None:
        """Serialize an action model to JSON and write it to the WebSocket."""
        if self._ws is None:
            logger.warning(
                "Tentacle %s: cannot send, WebSocket not connected", self.name
            )
            return

        if isinstance(action, (SendGroupMsgAction, SendPrivateMsgAction)):
            await self.prepare_outbound_message(action.params.message)

        frame = action.model_dump_json(exclude_none=True)
        try:
            await self._ws.send(frame)
        except ConnectionClosed:
            logger.warning("Tentacle %s: WebSocket closed while sending", self.name)

    async def download_image(self, seg: ImageSegment, save_dir: Path) -> None:
        try:
            url = seg.data.url
            if not url:
                resp = await self.http_client.post(
                    "/get_image", json={"file": str(seg.data.file)}
                )
                resp.raise_for_status()
                url = resp.json().get("data", {}).get("url")
            if not url:
                return

            resp = await self.http_client.get(url)
            resp.raise_for_status()

            ext = guess_image_ext(resp.headers.get("content-type", ""), url)
            path = save_dir / f"{uuid.uuid4().hex}{ext}"
            await anyio.Path(path).write_bytes(resp.content)

            seg.data.file = str(path.resolve())
            seg.data.url = url
        except Exception:
            logger.warning(
                "Tentacle %s: failed to download image", self.name, exc_info=True
            )

    async def prepare_inbound_message(self, event: MessageEvent) -> None:
        subdir = (
            f"g{event.group_id}"
            if isinstance(event, GroupMessageEvent)
            else f"u{event.user_id}"
        )
        save_dir = FILES_ROOT / self.name / subdir

        image_segs = [seg for seg in event.message if isinstance(seg, ImageSegment)]
        if not image_segs:
            return

        await anyio.Path(save_dir).mkdir(parents=True, exist_ok=True)
        async with anyio.create_task_group() as tg:
            for seg in image_segs:
                tg.start_soon(self.download_image, seg, save_dir)

    async def prepare_outbound_message(self, segments: list[AgentSegment]) -> None:
        for seg in segments:
            if not isinstance(seg, ImageSegment):
                continue
            try:
                apath = anyio.Path(seg.data.path)
                if await apath.exists():
                    data = await apath.read_bytes()
                    seg.data.file = f"base64://{base64.b64encode(data).decode()}"
            except Exception:
                logger.warning(
                    "Tentacle %s: failed to prepare outbound image", self.name
                )

    async def _connect_loop(self) -> None:
        """Connect with exponential back-off, delegating to _receive_loop."""
        delay = self.config.backoff_base
        while True:
            try:
                extra_headers = {}
                if self.config.access_token:
                    extra_headers["Authorization"] = (
                        f"Bearer {self.config.access_token.get_secret_value()}"
                    )

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

            frame.tentacle_id = self.name

            if isinstance(frame, MessageEvent):
                await self.prepare_inbound_message(frame)

            await self.nerve.sense(frame)
