from __future__ import annotations

import asyncio
import functools
import logging
import pickle
from collections import defaultdict, deque
from pathlib import Path

from mem0 import Memory
from pydantic_ai import Agent, ModelMessage
from pydantic_ai.messages import ModelRequest, UserPromptPart

from octomate.agents import SessionContext, create_companion_agent
from octomate.agents.manager import SkillManager
from octomate.config import MindConfig
from octomate.nerve import OctopusNerve
from octomate.schemas.actions import (
    AgentMessage,
    SendGroupMsgAction,
    SendGroupMsgParams,
    SendPrivateMsgAction,
    SendPrivateMsgParams,
)
from octomate.schemas.events import GroupMessageEvent, MessageEvent
from octomate.schemas.session import SessionKey
from octomate.tentacles.base import BaseTentacle

logger = logging.getLogger(__name__)


class Octopus:
    nerve: OctopusNerve
    agent: Agent[SessionContext, list[AgentMessage]]
    message_store: dict[SessionKey, deque[list[ModelMessage]]]
    memory: Memory | None
    _max_messages: int
    _store_path: Path

    def __init__(
        self,
        nerve: OctopusNerve,
        brain: MindConfig,
        skill_manager: SkillManager | None = None,
        store_path: Path = Path(".octomate/message_store"),
    ) -> None:
        self.nerve = nerve
        self.agent = create_companion_agent(brain, skill_manager)
        self._max_messages = brain.memory.max_messages
        self._store_path = store_path
        self.message_store = self._load_store()
        self.memory = Memory(brain.memory.mem0) if brain.memory.mem0.enabled else None

    def _load_store(self) -> dict[SessionKey, deque[list[ModelMessage]]]:
        store = defaultdict(lambda: deque(maxlen=self._max_messages))
        if self._store_path.exists():
            try:
                store.update(pickle.loads(self._store_path.read_bytes()))
            except Exception:
                logger.warning(
                    "Failed to load message store, starting fresh", exc_info=True
                )
        logger.info("Loaded message store from %s", self._store_path)
        return store

    def _save_store(self) -> None:
        self._store_path.parent.mkdir(parents=True, exist_ok=True)
        self._store_path.write_bytes(pickle.dumps(dict(self.message_store)))
        logger.info("Saved message store to %s", self._store_path)

    def connect(self, tentacle: BaseTentacle) -> None:
        self.nerve.connect(tentacle)

    async def activate(self) -> None:
        try:
            async with asyncio.TaskGroup() as tg:
                tg.create_task(self.nerve.activate())
                tg.create_task(self.receive())
        except* Exception as eg:
            for exc in eg.exceptions:
                logger.exception("Fatal error in task group", exc_info=exc)
        finally:
            self._save_store()

    async def receive(self) -> None:
        async with asyncio.TaskGroup() as tg, self.nerve.inbound:
            async for key, batch in self.nerve.inbound:
                try:
                    tentacle = self.nerve[key.tentacle_id]
                    if key.group_id is not None and not any(
                        isinstance(msg, GroupMessageEvent)
                        and msg.is_at(tentacle.self_id)
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
        tentacle = self.nerve[key.tentacle_id]
        identity = f"{tentacle.self_id}-{tentacle.self_name}"

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

        logger.debug("Octopus processing batch [%s] (%d messages)", key, len(batch))
        deps = SessionContext(nerve=self.nerve, session_key=key)
        result = await self.agent.run(
            user_prompt,
            message_history=history,
            deps=deps,
        )
        self.message_store[key].append(result.new_messages())

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
            await self.nerve.pulse(action)

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
