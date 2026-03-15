from __future__ import annotations

import asyncio
import functools
import logging
import os
import pickle
from collections import defaultdict, deque
from pathlib import Path

import anyio
from anyio import create_memory_object_stream as object_stream
from anyio.abc import ObjectReceiveStream, ObjectSendStream
from mem0 import Memory
from pydantic_ai import Agent, ModelMessage
from pydantic_ai.messages import ModelRequest, UserPromptPart

from octomate.agents import SessionContext, create_companion_agent
from octomate.agents.manager import SkillManager
from octomate.config import MindConfig
from octomate.schemas.actions import (
    AgentMessage,
    SendGroupMsgAction,
    SendGroupMsgParams,
    SendPrivateMsgAction,
    SendPrivateMsgParams,
)
from octomate.schemas.events import GroupMessageEvent, MessageEvent
from octomate.schemas.session import SessionKey
from octomate.tentacles.base import Tentacle

logger = logging.getLogger(__name__)


class Octopus:
    agent: Agent[SessionContext, list[AgentMessage]]
    message_store: dict[SessionKey, deque[list[ModelMessage]]]
    memory: Memory | None
    tentacles: dict[str, Tentacle]
    _nerve_send: ObjectSendStream[tuple[SessionKey, list[MessageEvent]]]
    _nerve_receive: ObjectReceiveStream[tuple[SessionKey, list[MessageEvent]]]
    max_messages: int
    store_path: Path

    def __init__(
        self,
        brain: MindConfig,
        skill_manager: SkillManager | None = None,
        store_path: Path = Path(".octomate/message_store"),
        buffer_size: int = 64,
    ) -> None:
        self.tentacles = {}
        self._nerve_send, self._nerve_receive = object_stream(buffer_size)
        if brain.base_url:
            os.environ.setdefault("GOOGLE_GEMINI_BASE_URL", brain.base_url)
        self.agent = create_companion_agent(brain, skill_manager)
        self.max_messages = brain.memory.max_messages
        self.store_path = store_path
        self.message_store = self._load_store()
        self.memory = Memory(brain.memory.mem0) if brain.memory.mem0.enabled else None

    def _load_store(self) -> dict[SessionKey, deque[list[ModelMessage]]]:
        store = defaultdict(lambda: deque(maxlen=self.max_messages))
        if self.store_path.exists():
            try:
                store.update(pickle.loads(self.store_path.read_bytes()))
            except Exception:
                logger.warning(
                    "Failed to load message store, starting fresh", exc_info=True
                )
        logger.info("Loaded message store from %s", self.store_path)
        return store

    def _save_store(self) -> None:
        self.store_path.parent.mkdir(parents=True, exist_ok=True)
        self.store_path.write_bytes(pickle.dumps(dict(self.message_store)))
        logger.info("Saved message store to %s", self.store_path)

    def connect(self, tentacle: Tentacle) -> None:
        if tentacle.tag in self.tentacles:
            raise ValueError(f"Tentacle {tentacle.tag!r} already connected")
        self.tentacles[tentacle.tag] = tentacle
        logger.info("Connected tentacle: %s", tentacle.tag)

    def cut(self, name: str) -> None:
        self.tentacles.pop(name, None)

    async def kick(self, key: SessionKey, batch: list[MessageEvent]) -> None:
        await self._nerve_send.send((key, batch))

    async def activate(self) -> None:
        try:
            async with asyncio.TaskGroup() as tg:
                tg.create_task(self.active_tentacles())
                tg.create_task(self.rolling())
        except* Exception as eg:
            for exc in eg.exceptions:
                logger.exception("Fatal error in task group", exc_info=exc)
        finally:
            self._save_store()

    async def active_tentacles(self) -> None:
        async with anyio.create_task_group() as tg:
            for tentacle in self.tentacles.values():
                tentacle.buffer.bind(tg)
            for name, tentacle in self.tentacles.items():
                tg.start_soon(tentacle.activate, name=f"tentacle:{name}")

    async def rolling(self) -> None:
        async with asyncio.TaskGroup() as tg, self._nerve_receive:
            async for key, batch in self._nerve_receive:
                try:
                    tentacle = self.tentacles[key.tentacle_id]
                    if key.group_id is not None and not any(
                        isinstance(msg, GroupMessageEvent) and msg.is_at(tentacle.id)
                        for msg in batch
                    ):
                        self.message_store[key].append(
                            [
                                ModelRequest(parts=[UserPromptPart(content=str(msg))])
                                for msg in batch
                            ]
                        )
                        continue
                    tg.create_task(self.think(key, batch))
                except Exception:
                    logger.exception("Error processing batch [%s]", key)

    async def memory_search(self, key: SessionKey, query: str) -> list[dict]:
        if self.memory:
            fn = functools.partial(self.memory.search, query, user_id=str(key), limit=5)
            result = await asyncio.to_thread(fn)
            return result.get("results", result) if isinstance(result, dict) else result
        return []

    async def memory_add(self, key: SessionKey, messages: list[dict]) -> None:
        if self.memory:
            try:
                fn = functools.partial(self.memory.add, messages, user_id=str(key))
                await asyncio.to_thread(fn)
            except Exception:
                logger.warning("Memory add failed", exc_info=True)

    async def think(self, key: SessionKey, batch: list[MessageEvent]) -> None:
        tentacle = self.tentacles[key.tentacle_id]
        identity = f"{tentacle.id}-{tentacle.name}"

        if key.group_id is not None:
            header = f"[group chat: {key.group_id}] [me: {identity}]"
        else:
            header = f"[private chat] [me: {identity}]"

        messages = [str(msg) for msg in batch]
        user_prompt: list = [header]
        for msg in batch:
            user_prompt.extend(msg.to_content_parts())

        history: list[ModelMessage] = [
            msg for batch in self.message_store[key] for msg in batch
        ]

        query = ""
        if self.memory:
            query = " ".join(messages).strip()
            if query:
                try:
                    memories = await self.memory_search(key, query)
                except Exception:
                    logger.warning(
                        "Memory search failed, proceeding without", exc_info=True
                    )
                    memories = []
                if memories:
                    facts = "\n".join(f"- {m['memory']}" for m in memories)
                    content = f"[relevant memories]\n{facts}"
                    history.append(
                        ModelRequest(parts=[UserPromptPart(content=content)])
                    )

        logger.info("Octopus processing batch [%s] (%d messages)", key, len(batch))
        deps = SessionContext(session_key=key)
        result = await self.agent.run(
            user_prompt,
            message_history=history,
            deps=deps,
        )
        self.message_store[key].append(result.new_messages())
        logger.info("Agent returned %d messages for [%s]", len(result.output), key)

        for msg in result.output:
            if key.group_id is not None:
                action = SendGroupMsgAction(
                    tentacle_id=key.tentacle_id,
                    params=SendGroupMsgParams(
                        group_id=key.group_id, message=msg.segments
                    ),
                )
            else:
                action = SendPrivateMsgAction(
                    tentacle_id=key.tentacle_id,
                    params=SendPrivateMsgParams(
                        user_id=key.user_id, message=msg.segments
                    ),
                )
            await tentacle.twitch(action)

        if self.memory and query:
            asyncio.create_task(
                self.memory_add(
                    key,
                    [
                        {
                            "role": "user",
                            "content": message,
                        }
                        for message in messages
                    ]
                    + [
                        {
                            "role": "assistant",
                            "content": f"[me: {identity}]: {str(msg)}",
                        }
                        for msg in result.output
                    ],
                )
            )
