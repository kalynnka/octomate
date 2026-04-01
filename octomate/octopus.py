from __future__ import annotations

import asyncio
import contextlib
import logging

from anyio import create_memory_object_stream as object_stream
from anyio.abc import ObjectReceiveStream, ObjectSendStream

from octomate.agents.manager import SkillManager
from octomate.schemas.events import MessageEvent
from octomate.schemas.session import SessionKey
from octomate.tentacles.base import AgentTentacle, ChannelTentacle
from octomate.transmuters import sqlalchemy_materia

logger = logging.getLogger(__name__)


class Octopus:
    skill_manager: SkillManager | None
    tentacles: dict[str, ChannelTentacle]
    agent_tentacles: dict[str, AgentTentacle]
    _nerve_send: ObjectSendStream[tuple[SessionKey, list[MessageEvent]]]
    _nerve_receive: ObjectReceiveStream[tuple[SessionKey, list[MessageEvent]]]

    def __init__(
        self,
        skill_manager: SkillManager | None = None,
        buffer_size: int = 64,
    ) -> None:
        self.tentacles = {}
        self.agent_tentacles = {}
        self.skill_manager = skill_manager
        self._nerve_send, self._nerve_receive = object_stream(buffer_size)

    async def activate(self) -> None:
        try:
            async with contextlib.AsyncExitStack() as stack:
                stack.enter_context(sqlalchemy_materia)
                if self.skill_manager:
                    await stack.enter_async_context(self.skill_manager)
                async with asyncio.TaskGroup() as tg:
                    for name, tentacle in self.tentacles.items():
                        tg.create_task(tentacle.activate(), name=f"tentacle:{name}")
                    tg.create_task(self.rolling())
        except* Exception as eg:
            for exc in eg.exceptions:
                logger.exception("Fatal error in task group", exc_info=exc)
        finally:
            for tentacle in self.tentacles.values():
                await tentacle.deactivate()

    def connect(self, tentacle: ChannelTentacle) -> None:
        if tentacle.id in self.tentacles:
            raise ValueError(f"Tentacle {tentacle.id!r} already connected")
        self.tentacles[tentacle.id] = tentacle
        logger.info("Connected tentacle: %s", tentacle.id)

    def graft(self, tentacle: AgentTentacle) -> None:
        if tentacle.id in self.agent_tentacles:
            raise ValueError(f"Agent tentacle {tentacle.id!r} already grafted")
        self.agent_tentacles[tentacle.id] = tentacle
        logger.info("Grafted agent tentacle: %s", tentacle.id)

    def cut(self, name: str) -> None:
        self.tentacles.pop(name, None)

    async def kick(self, key: SessionKey, batch: list[MessageEvent]) -> None:
        await self._nerve_send.send((key, batch))

    async def rolling(self) -> None:
        async with asyncio.TaskGroup() as tg, self._nerve_receive:
            async for key, batch in self._nerve_receive:
                channel = self.tentacles[key.tentacle_id]
                owner_id = await channel.threads.get_owner(key)
                owner = self.agent_tentacles.get(owner_id)
                if owner:
                    tg.create_task(owner(key, batch))
                else:
                    await channel.threads.set_owner(key, channel)
                    tg.create_task(channel(key, batch))
