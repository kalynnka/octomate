from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import anyio
from anyio import create_memory_object_stream as object_stream
from anyio.abc import ObjectReceiveStream, ObjectSendStream

from octomate.schemas.adaptors import ActionUnion
from octomate.schemas.events import MessageEvent, OneBotEventUnion, SessionKey
from octomate.tentacles.base import MessageBuffer

if TYPE_CHECKING:
    from octomate.tentacles.base import BaseTentacle

logger = logging.getLogger(__name__)


class OctopusNerve:
    _inbound_send: ObjectSendStream[tuple[SessionKey, list[MessageEvent]]]
    _inbound_receive: ObjectReceiveStream[tuple[SessionKey, list[MessageEvent]]]
    _outbound_send: ObjectSendStream[ActionUnion]
    _outbound_receive: ObjectReceiveStream[ActionUnion]

    @property
    def inbound(self) -> ObjectReceiveStream[tuple[SessionKey, list[MessageEvent]]]:
        return self._inbound_receive

    @property
    def outbound(self) -> ObjectReceiveStream[ActionUnion]:
        return self._outbound_receive

    def __init__(self, buffer_size: int = 64, flush_delay: float = 0.5) -> None:
        self._inbound_send, self._inbound_receive = object_stream(buffer_size)
        self._outbound_send, self._outbound_receive = object_stream(buffer_size)
        self._tentacles: dict[str, BaseTentacle] = {}
        self._buffer = MessageBuffer(
            flush_delay=flush_delay,
            handler=lambda key, batch: self._inbound_send.send((key, batch)),
        )

    def __getitem__(self, name: str) -> BaseTentacle:
        """Look up a tentacle by name, raising ``KeyError`` if not found."""
        try:
            return self._tentacles[name]
        except KeyError:
            raise KeyError(f"No tentacle named {name!r}") from None

    def connect(self, tentacle: BaseTentacle) -> None:
        """Add a tentacle and bind its nerve reference."""
        if tentacle.name in self._tentacles:
            raise ValueError(f"Tentacle {tentacle.name!r} already connected")
        tentacle.nerve = self
        self._tentacles[tentacle.name] = tentacle
        logger.info("Connected tentacle: %s", tentacle.name)

    def cut(self, name: str) -> None:
        """Remove a tentacle from the registry."""
        self._tentacles.pop(name, None)

    def get(self, name: str) -> BaseTentacle | None:
        """Look up a tentacle by name, returning ``None`` if not found."""
        return self._tentacles.get(name)

    async def activate(self) -> None:
        async with anyio.create_task_group() as tg:
            self._buffer.bind(tg)
            for tentacle in self._tentacles.values():
                tg.start_soon(tentacle.activate, name=f"tentacle:{tentacle.name}")
            tg.start_soon(self._react_pulses, name="outbound-dispatcher")

    async def deactivate(self) -> None:
        """Stop all tentacles and close streams."""
        for tentacle in self._tentacles.values():
            try:
                await tentacle.deactivate()
            except Exception:
                logger.exception("Error stopping tentacle %s", tentacle.name)
        await self._inbound_send.aclose()
        await self._inbound_receive.aclose()
        await self._outbound_send.aclose()
        await self._outbound_receive.aclose()

    async def sense(self, event: OneBotEventUnion) -> None:
        if isinstance(event, MessageEvent):
            self._buffer.push(event)

    async def pulse(self, action: ActionUnion) -> None:
        await self._outbound_send.send(action)

    async def _react_pulses(self) -> None:
        logger.info("Outbound dispatcher started")
        async with self.outbound:
            async for action in self.outbound:
                tentacle_id = action.tentacle_id
                tentacle = self._tentacles.get(tentacle_id)
                if tentacle is None:
                    logger.warning(
                        "No tentacle registered for id %r, dropping action",
                        tentacle_id,
                    )
                    continue

                try:
                    await tentacle.act(action)
                except Exception:  # noqa: BLE001
                    logger.exception(
                        "Error sending action via tentacle %s", tentacle_id
                    )
