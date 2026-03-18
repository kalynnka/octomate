from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING

from pydantic_ai.messages import ModelMessage
from zep_cloud.client import AsyncZep

from octomate.memory.base import OctopusMemory
from octomate.schemas.session import SessionKey

if TYPE_CHECKING:
    from octomate.schemas.events import MessageEvent
    from octomate.tentacles.base import Tentacle

logger = logging.getLogger(__name__)

BOT_USER_ID = "octomate"


class ZepMemory(OctopusMemory):
    client: AsyncZep
    known_users: set[str]
    known_graphs: set[str]

    def __init__(
        self,
        api_key: str,
        bot_name: str = "Octomate",
        max_messages: int = 32,
        store_path: Path = Path(".octomate/message_store"),
    ) -> None:
        super().__init__(max_messages=max_messages, store_path=store_path)
        self.client = AsyncZep(api_key=api_key)
        self.bot_name = bot_name
        self.known_users = set()
        self.known_graphs = set()

    async def ensure_user(self, user_id: str, first_name: str | None = None) -> None:
        if user_id in self.known_users:
            return
        try:
            await self.client.user.get(user_id)
        except Exception:
            await self.client.user.add(
                user_id=user_id, first_name=first_name or user_id
            )
        self.known_users.add(user_id)

    async def ensure_graph(self, graph_id: str) -> None:
        if graph_id in self.known_graphs:
            return
        try:
            await self.client.graph.get(graph_id)
        except Exception:
            await self.client.graph.create(graph_id=graph_id)
        self.known_graphs.add(graph_id)

    @staticmethod
    def session_graph_id(key: SessionKey) -> str:
        if key.group_id is not None:
            return f"{key.tentacle_id}:group:{key.group_id}"
        return f"{key.tentacle_id}:private:{key.user_id}"

    async def recall(self, key: SessionKey, query: str, limit: int = 5) -> list[str]:
        if not query:
            return []
        user_id = str(key.user_id)
        graph_id = self.session_graph_id(key)
        facts: list[str] = []
        half = max(limit // 2, 1)

        for search_kwargs in [
            {"user_id": user_id, "limit": half},
            {"graph_id": graph_id, "limit": limit - half},
        ]:
            try:
                result = await self.client.graph.search(
                    query=query, scope="edges", **search_kwargs
                )
                for e in result.edges or []:
                    if e.fact and e.fact not in facts:
                        facts.append(e.fact)
            except Exception:
                logger.warning("Zep recall failed for %s", search_kwargs, exc_info=True)
        return facts[:limit]

    async def memo(
        self,
        key: SessionKey,
        messages: list[ModelMessage],
        events: list[MessageEvent] | None = None,
        tentacle: Tentacle | None = None,
    ) -> None:
        dicts = self.messages_to_dicts(messages)
        if not dicts:
            return

        graph_id = self.session_graph_id(key)
        user_id = str(key.user_id)

        try:
            await self.ensure_graph(graph_id)
            await self.ensure_user(BOT_USER_ID, self.bot_name)

            if tentacle and events:
                for event in events:
                    eid = str(event.user_id)
                    if eid not in self.known_users:
                        profile = await tentacle.get_user_profile(eid)
                        await self.ensure_user(eid, profile.nickname)

            data = "\n".join(f"{m['role']}: {m['content']}" for m in dicts)

            await self.client.graph.add(data=data, type="text", user_id=user_id)
            await self.client.graph.add(data=data, type="text", graph_id=graph_id)
        except Exception:
            logger.warning("Zep memo failed", exc_info=True)
