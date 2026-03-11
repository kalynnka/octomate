"""Nerve — central message relay connecting all tentacles.

Named after the octopus's decentralized nervous system.  The Nerve
uses two pairs of ``anyio.create_memory_object_stream`` to decouple
tentacles (inbound events) from the processing core (outbound actions).
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Annotated, Union

import anyio
from anyio import create_memory_object_stream as object_stream
from anyio.abc import ObjectReceiveStream, ObjectSendStream
from pydantic import Discriminator, Tag

from octomate.schemas.events import (
    CallApiAction,
    OneBotEventUnion,
    SendGroupMsgAction,
    SendPrivateMsgAction,
)

if TYPE_CHECKING:
    from octomate.tentacles.base import BaseTentacle

logger = logging.getLogger(__name__)

ActionUnion = Annotated[
    Union[
        Annotated[SendGroupMsgAction, Tag("send_group_msg")],
        Annotated[SendPrivateMsgAction, Tag("send_private_msg")],
        Annotated[CallApiAction, Tag("__default__")],
    ],
    Discriminator("action"),
]


class Nerve:
    """Central relay between tentacles and the processing core.

    Manages tentacle lifecycle, routes outbound actions, and provides
    bounded async streams for inbound events and outbound actions.
    """

    _inbound_send: ObjectSendStream[OneBotEventUnion]
    _inbound_receive: ObjectReceiveStream[OneBotEventUnion]
    _outbound_send: ObjectSendStream[ActionUnion]
    _outbound_receive: ObjectReceiveStream[ActionUnion]

    def __init__(self, buffer_size: int = 64) -> None:
        self._inbound_send, self._inbound_receive = object_stream(buffer_size)
        self._outbound_send, self._outbound_receive = object_stream(buffer_size)
        self._tentacles: dict[str, BaseTentacle] = {}

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

    def __getitem__(self, name: str) -> BaseTentacle:
        """Look up a tentacle by name, raising ``KeyError`` if not found."""
        try:
            return self._tentacles[name]
        except KeyError:
            raise KeyError(f"No tentacle named {name!r}") from None

    async def activate(self) -> None:
        """Start all tentacles and the outbound dispatcher."""
        async with anyio.create_task_group() as tg:
            for tentacle in self._tentacles.values():
                tg.start_soon(tentacle.start, name=f"tentacle:{tentacle.name}")
            tg.start_soon(self._dispatch_outbound, name="outbound-dispatcher")

    async def deactivate(self) -> None:
        """Stop all tentacles and close streams."""
        for tentacle in self._tentacles.values():
            try:
                await tentacle.stop()
            except Exception:
                logger.exception("Error stopping tentacle %s", tentacle.name)
        await self._inbound_send.aclose()
        await self._inbound_receive.aclose()
        await self._outbound_send.aclose()
        await self._outbound_receive.aclose()

    async def publish_inbound(self, event: OneBotEventUnion) -> None:
        """Push an event from a tentacle into the inbound stream."""
        await self._inbound_send.send(event)

    async def consume_inbound(self) -> OneBotEventUnion:
        """Block until the next inbound event is available."""
        return await self._inbound_receive.receive()

    async def publish_outbound(self, action: ActionUnion) -> None:
        """Push an action from the processor into the outbound stream."""
        await self._outbound_send.send(action)

    async def consume_outbound(self) -> ActionUnion:
        """Block until the next outbound action is available."""
        return await self._outbound_receive.receive()

    async def _dispatch_outbound(self) -> None:
        """Consume outbound actions and route to tentacles."""
        logger.info("Outbound dispatcher started")
        while True:
            try:
                action = await self.consume_outbound()
            except anyio.EndOfStream:
                logger.info("Outbound stream closed, dispatcher exiting")
                break

            tentacle_id = action.tentacle_id
            tentacle = self._tentacles.get(tentacle_id)
            if tentacle is None:
                logger.warning(
                    "No tentacle registered for id %r, dropping action",
                    tentacle_id,
                )
                continue

            try:
                await tentacle.send(action)
            except Exception:  # noqa: BLE001
                logger.exception("Error sending action via tentacle %s", tentacle_id)
