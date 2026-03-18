from __future__ import annotations

import asyncio
import logging
import os

import anyio
from anyio import create_memory_object_stream as object_stream
from anyio.abc import ObjectReceiveStream, ObjectSendStream
from pydantic_ai import Agent
from pydantic_ai.messages import ModelRequest, UserPromptPart

from octomate.agents import SessionContext, create_companion_agent
from octomate.agents.manager import SkillManager
from octomate.config import MindConfig
from octomate.memory.base import OctopusMemory
from octomate.schemas.actions import AgentMessage
from octomate.schemas.events import GroupMessageEvent, MessageEvent
from octomate.schemas.session import SessionKey
from octomate.tentacles.base import SendTarget, Tentacle

logger = logging.getLogger(__name__)


class Octopus:
    agent: Agent[SessionContext, list[AgentMessage]]
    memory: OctopusMemory
    tentacles: dict[str, Tentacle]
    _nerve_send: ObjectSendStream[tuple[SessionKey, list[MessageEvent]]]
    _nerve_receive: ObjectReceiveStream[tuple[SessionKey, list[MessageEvent]]]

    def __init__(
        self,
        brain: MindConfig,
        memory: OctopusMemory,
        skill_manager: SkillManager | None = None,
        buffer_size: int = 64,
    ) -> None:
        self.tentacles = {}
        self.memory = memory
        self._nerve_send, self._nerve_receive = object_stream(buffer_size)
        if brain.base_url:
            os.environ.setdefault("GOOGLE_GEMINI_BASE_URL", brain.base_url)
        self.agent = create_companion_agent(brain, skill_manager)

    async def activate(self) -> None:
        try:
            async with asyncio.TaskGroup() as tg:
                tg.create_task(self.active_tentacles())
                tg.create_task(self.rolling())
        except* Exception as eg:
            for exc in eg.exceptions:
                logger.exception("Fatal error in task group", exc_info=exc)
        finally:
            self.memory.save()

    async def active_tentacles(self) -> None:
        async with anyio.create_task_group() as tg:
            for tentacle in self.tentacles.values():
                tentacle.buffer.bind(tg)
            for name, tentacle in self.tentacles.items():
                tg.start_soon(tentacle.activate, name=f"tentacle:{name}")

    def connect(self, tentacle: Tentacle) -> None:
        if tentacle.tag in self.tentacles:
            raise ValueError(f"Tentacle {tentacle.tag!r} already connected")
        self.tentacles[tentacle.tag] = tentacle
        logger.info("Connected tentacle: %s", tentacle.tag)

    def cut(self, name: str) -> None:
        self.tentacles.pop(name, None)

    async def kick(self, key: SessionKey, batch: list[MessageEvent]) -> None:
        await self._nerve_send.send((key, batch))

    async def rolling(self) -> None:
        async with asyncio.TaskGroup() as tg, self._nerve_receive:
            async for key, batch in self._nerve_receive:
                try:
                    tentacle = self.tentacles[key.tentacle_id]
                    if key.group_id is not None and not any(
                        isinstance(msg, GroupMessageEvent) and msg.is_at(tentacle.id)
                        for msg in batch
                    ):
                        self.memory.record(
                            key,
                            [
                                ModelRequest(parts=[UserPromptPart(content=str(msg))])
                                for msg in batch
                            ],
                        )
                        continue
                    tg.create_task(self.think(key, batch))
                except Exception:
                    logger.exception("Error processing batch [%s]", key)

    async def think(self, key: SessionKey, batch: list[MessageEvent]) -> None:
        tentacle = self.tentacles[key.tentacle_id]
        profile = tentacle.profile

        if key.group_id is not None:
            header = f"[me: {profile.name} ({profile.user_id})] [group: {key.group_id}]"
        else:
            header = f"[me: {profile.name} ({profile.user_id})] [chat: private]"

        user_prompt: list = [header]
        for msg in batch:
            user_prompt.extend(msg.to_content_parts())

        history = self.memory.history(key)

        if batch:
            memories = await self.memory.recall(key, batch, tentacle)
            if memories:
                facts = "\n".join(f"- {m}" for m in memories)
                content = f"[relevant memories]\n{facts}"
                history.append(ModelRequest(parts=[UserPromptPart(content=content)]))

        logger.info("Octopus processing batch [%s] (%d messages)", key, len(batch))

        if key.group_id is not None:
            target = SendTarget("group", key.group_id)
        else:
            target = SendTarget("private", key.user_id)

        deps = SessionContext(session_key=key, tentacle=tentacle)
        result = await self.agent.run(
            user_prompt,
            message_history=history,
            deps=deps,
        )
        self.memory.record(key, result.new_messages())
        logger.info("Agent returned %d messages for [%s]", len(result.output), key)

        for msg in result.output:
            await tentacle.twitch(target, msg.segments)

        asyncio.create_task(self.memory.memo(key, result.output, tentacle))
