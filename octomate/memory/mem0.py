from __future__ import annotations

import asyncio
import functools
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any

from mem0 import Memory as Mem0

from octomate.memory.base import OctopusMemory
from octomate.schemas.actions import AgentMessage
from octomate.schemas.session import SessionKey

if TYPE_CHECKING:
    from octomate.schemas.events import MessageEvent
    from octomate.tentacles.base import Tentacle

logger = logging.getLogger(__name__)


class Mem0Memory(OctopusMemory):
    mem0: Mem0

    def __init__(
        self,
        max_messages: int = 32,
        history_size: int = 16,
        store_path: Path = Path(".octomate/message_store"),
        **mem0_kwargs: Any,
    ) -> None:
        super().__init__(
            max_messages=max_messages, history_size=history_size, store_path=store_path
        )
        self.mem0 = Mem0(**mem0_kwargs)

    @staticmethod
    def run_id(key: SessionKey) -> str:
        if key.group_id is not None:
            return f"{key.tentacle_id}:group:{key.group_id}"
        return f"{key.tentacle_id}:private:{key.user_id}"

    async def recall(
        self,
        key: SessionKey,
        events: list[MessageEvent],
        tentacle: Tentacle,
        limit: int = 5,
    ) -> list[str]:
        query = " ".join(str(e) for e in events).strip()
        if not query:
            return []

        try:
            rid = self.run_id(key)

            await asyncio.gather(
                *(
                    asyncio.to_thread(
                        functools.partial(
                            self.mem0.add,
                            [{"role": "user", "content": content}],
                            user_id=str(event.user_id),
                            run_id=rid,
                        )
                    )
                    for event in events
                    if (content := str(event))
                )
            )

            result = await asyncio.to_thread(
                functools.partial(
                    self.mem0.search,
                    query,
                    run_id=rid,
                    limit=limit,
                )
            )
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
        messages: list[AgentMessage],
        tentacle: Tentacle,
    ) -> None:
        if not messages:
            return
        rid = self.run_id(key)
        dicts = [{"role": "assistant", "content": str(msg)} for msg in messages]
        try:
            fn = functools.partial(
                self.mem0.add,
                dicts,
                user_id=tentacle.profile.user_id,
                run_id=rid,
            )
            await asyncio.to_thread(fn)
        except Exception:
            logger.warning("Memory memo failed", exc_info=True)
