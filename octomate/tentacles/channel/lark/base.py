from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from typing import TYPE_CHECKING, Any

import lark_oapi
import lark_oapi.ws.client as ws_mod
from lark_oapi.api.im.v1.model.p2_im_message_receive_v1 import P2ImMessageReceiveV1
from pydantic import SecretStr
from pydantic_ai import AgentRunResultEvent, AgentStreamEvent

from octomate.schemas.conversation import ConversationKey
from octomate.schemas.events import MessageEvent
from octomate.tentacles.channel.base import ChannelTentacle
from octomate.tentacles.channel.lark.chromo import LarkChromo
from octomate.tentacles.channel.lark.ink import LarkInk

if TYPE_CHECKING:
    from octomate.base import Octomate

logger = logging.getLogger(__name__)


class LarkTentacle(ChannelTentacle):
    ws_client: lark_oapi.ws.Client
    stop_event: asyncio.Event | None
    ping_task: asyncio.Task[None] | None

    def __init__(
        self,
        id: str,
        octomate: Octomate | None,
        *,
        app_id: str,
        app_secret: SecretStr,
        agent_id: str = "inkling",
        mention_only: bool = True,
    ) -> None:
        self.ink = LarkInk(app_id, app_secret)
        self.chromo = LarkChromo()

        event_handler = (
            lark_oapi.EventDispatcherHandler.builder("", "")
            .register_p2_im_message_receive_v1(self.sense)
            .build()
        )
        self.ws_client = lark_oapi.ws.Client(
            self.ink.app_id,
            self.ink.app_secret.get_secret_value(),
            event_handler=event_handler,
            log_level=lark_oapi.LogLevel.INFO,
        )
        self.stop_event = None
        self.ping_task = None
        super().__init__(
            id=id,
            octomate=octomate,
            ink=self.ink,
            chromo=self.chromo,
            agent_id=agent_id,
            mention_only=mention_only,
        )

    async def activate(self) -> None:
        logger.info("Channel %s: starting Lark WebSocket client", self.id)
        # lark-oapi exposes a blocking public start(), but its WebSocket client
        # is async internally. Run those internals on Octomate's event loop so
        # message callbacks can schedule ingest directly.
        ws_mod.loop = asyncio.get_running_loop()
        self.stop_event = asyncio.Event()
        await self.ws_client._connect()  # type: ignore[attr-defined]
        self.ping_task = asyncio.create_task(self.ws_client._ping_loop())  # type: ignore[attr-defined]
        try:
            await self.stop_event.wait()
        finally:
            await self._disconnect()

    async def deactivate(self) -> None:
        if self.stop_event is not None:
            self.stop_event.set()
        await self._disconnect()

    async def respond(
        self,
        key: ConversationKey,
        events: AsyncIterator[AgentStreamEvent | AgentRunResultEvent[Any]],
        *,
        source_events: list[MessageEvent] | None = None,
    ) -> None:
        chat_id = key.chat_id or key.user_id
        reply_to = self.reply_target_from_source_events(source_events)
        reply_in_thread = key.chat_type == "group" and reply_to is not None
        async for message in self.chromo.squirt(events, reply_to=reply_to):
            await self.ink.send_message(
                chat_id,
                key.chat_type,
                [message],
                reply_to,
                reply_in_thread=reply_in_thread,
            )

    def sense(self, data: P2ImMessageReceiveV1) -> None:
        task = asyncio.create_task(self.ingest(data))
        task.add_done_callback(self._log_ingest_result)

    async def close(self) -> None:
        await self.deactivate()
        self.ink.close()

    async def _disconnect(self) -> None:
        self.ws_client._auto_reconnect = False  # type: ignore[attr-defined]
        if self.ping_task is not None:
            ping_task = self.ping_task
            self.ping_task = None
            ping_task.cancel()
            try:
                await ping_task
            except asyncio.CancelledError:
                pass
        await self.ws_client._disconnect()  # type: ignore[attr-defined]

    def _log_ingest_result(self, task: asyncio.Task[None]) -> None:
        try:
            task.result()
        except asyncio.CancelledError:
            return
        except Exception:
            logger.exception("Channel %s: failed to handle Lark message", self.id)

    def reply_target_from_source_events(
        self,
        source_events: list[MessageEvent] | None,
    ) -> str | None:
        for event in reversed(source_events or ()):
            if event.message_id.startswith("om_"):
                return event.message_id
            if event.reply_id.startswith("om_"):
                return event.reply_id
        return None
