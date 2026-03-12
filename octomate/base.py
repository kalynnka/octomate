from __future__ import annotations

import logging
from collections import defaultdict, deque

import anyio
from pydantic_ai import Agent, ModelMessage

from octomate.agents import SessionContext, create_companion_agent
from octomate.config import BrainConfig
from octomate.nerve import OctopusNerve
from octomate.schemas.events import MessageEvent
from octomate.schemas.session import SessionKey
from octomate.tentacles.base import BaseTentacle

logger = logging.getLogger(__name__)


class Octopus:
    nerve: OctopusNerve
    agent: Agent[SessionContext, str]
    memory_store: dict[SessionKey, deque[ModelMessage]]

    def __init__(self, nerve: OctopusNerve, brain: BrainConfig) -> None:
        self.nerve = nerve
        self.agent = create_companion_agent(brain)
        self.memory_store = defaultdict(lambda: deque(maxlen=brain.memory.max_messages))

    def connect(self, tentacle: BaseTentacle) -> None:
        self.nerve.connect(tentacle)

    async def activate(self) -> None:
        try:
            async with anyio.create_task_group() as tg:
                tg.start_soon(self.nerve.activate)
                tg.start_soon(self.receive)
        except* Exception as eg:
            for exc in eg.exceptions:
                logger.exception("Fatal error in task group", exc_info=exc)

    async def receive(self) -> None:
        async with self.nerve.inbound:
            async for key, batch in self.nerve.inbound:
                try:
                    await self.think(key, batch)
                except Exception:
                    logger.exception("Error processing batch [%s]", key)

    async def think(self, key: SessionKey, batch: list[MessageEvent]) -> None:
        tentacle = self.nerve.get(key.tentacle_id)
        identity = (
            str(tentacle.self_id) if tentacle and tentacle.self_id else key.tentacle_id
        )
        if key.group_id is not None:
            header = f"[group chat: {key.group_id}] [you are user: {identity}]"
        else:
            header = f"[private chat] [you are user: {identity}]"
        messages = "\n".join(f"- {msg}" for msg in batch)
        text = f"{header}\n{messages}"

        prompt: list[str] = [text]

        logger.debug("Octopus processing batch [%s] (%d messages)", key, len(batch))
        deps = SessionContext(nerve=self.nerve, session_key=key)
        history = list(self.memory_store[key])
        result = await self.agent.run(prompt, message_history=history, deps=deps)
        self.memory_store[key].extend(result.new_messages())
