from __future__ import annotations

import logging

import anyio
from pydantic_ai import Agent

from octomate.agents import SessionContext, create_companion_agent
from octomate.config import BrainConfig
from octomate.nerve import OctopusNerve
from octomate.schemas.events import MessageEvent
from octomate.schemas.session import SessionKey
from octomate.tentacles.base import BaseTentacle

logger = logging.getLogger(__name__)


class Octopus:
    def __init__(self, nerve: OctopusNerve, brain: BrainConfig) -> None:
        self.nerve = nerve
        self.agent: Agent[SessionContext, str] = create_companion_agent(brain)

    def connect(self, tentacle: BaseTentacle) -> None:
        self.nerve.connect(tentacle)

    async def activate(self) -> None:
        async with anyio.create_task_group() as tg:
            tg.start_soon(self.nerve.activate)
            tg.start_soon(self.receive)

    async def receive(self) -> None:
        async with self.nerve.inbound:
            async for key, batch in self.nerve.inbound:
                await self.think(key, batch)

    async def think(self, key: SessionKey, batch: list[MessageEvent]) -> None:
        if key.group_id is not None:
            header = f"[group chat: {key.group_id}] [you are: {key.tentacle_id}]"
        else:
            header = "[private chat]"
        messages = "\n".join(f"- {msg}" for msg in batch)
        prompt = f"{header}\n{messages}"
        logger.debug("Octopus processing batch [%s] (%d messages)", key, len(batch))
        deps = SessionContext(nerve=self.nerve, session_key=key)
        await self.agent.run(prompt, deps=deps)
