from __future__ import annotations

import asyncio
import functools
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any

from mem0 import Memory as Mem0
from pydantic_ai.messages import ModelMessage

from octomate.memory.base import OctopusMemory
from octomate.schemas.session import SessionKey

if TYPE_CHECKING:
    from octomate.schemas.events import MessageEvent
    from octomate.tentacles.base import Tentacle

logger = logging.getLogger(__name__)


class Mem0Memory(OctopusMemory):
    provider: Mem0

    def __init__(
        self,
        max_messages: int = 32,
        store_path: Path = Path(".octomate/message_store"),
        **mem0_kwargs: Any,
    ) -> None:
        super().__init__(max_messages=max_messages, store_path=store_path)
        self.provider = Mem0(**mem0_kwargs)

    async def recall(self, key: SessionKey, query: str, limit: int = 5) -> list[str]:
        if not query:
            return []
        fn = functools.partial(
            self.provider.search,
            query,
            user_id=str(key),
            limit=limit,
        )
        try:
            result = await asyncio.to_thread(fn)
            items = (
                result.get("results", result) if isinstance(result, dict) else result
            )
            return [m["memory"] for m in items if "memory" in m]
        except Exception:
            logger.warning("Memory recall failed", exc_info=True)
            return []

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
        try:
            fn = functools.partial(self.provider.add, dicts, user_id=str(key))
            await asyncio.to_thread(fn)
        except Exception:
            logger.warning("Memory memo failed", exc_info=True)
