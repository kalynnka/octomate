from __future__ import annotations

import asyncio
import functools
import logging
from typing import TYPE_CHECKING, Any

from mem0 import Memory as Mem0

from octomate.memory.base import OctopusMemory
from octomate.schemas.actions import AgentMessage
from octomate.schemas.events import MessageEvent
from octomate.schemas.session import SessionKey

if TYPE_CHECKING:
    from octomate.tentacles.base import ChannelTentacle

logger = logging.getLogger(__name__)


class Mem0Memory(OctopusMemory):
    mem0: Mem0

    def __init__(
        self,
        **mem0_kwargs: Any,
    ) -> None:
        super().__init__()
        self.mem0 = Mem0(**mem0_kwargs)

    @staticmethod
    def run_id(key: SessionKey) -> str:
        if key.group_id:
            return f"{key.tentacle_id}:group:{key.group_id}"
        return f"{key.tentacle_id}:private:{key.user_id}"

    async def recall(
        self,
        key: SessionKey,
        events: list[MessageEvent],
        tentacle: ChannelTentacle,
        limit: int = 5,
    ) -> list[str]:
        query = " ".join(str(e) for e in events).strip()
        if not query:
            return []

        try:
            await self.memo(key, events, tentacle)

            rid = self.run_id(key)
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
        messages: list[AgentMessage] | list[MessageEvent],
        tentacle: ChannelTentacle,
    ) -> None:
        await super().memo(key, messages, tentacle)
        if not messages:
            return
        rid = self.run_id(key)
        try:
            await asyncio.gather(
                *(
                    asyncio.to_thread(
                        functools.partial(
                            self.mem0.add,
                            [
                                {
                                    "role": "user"
                                    if isinstance(msg, MessageEvent)
                                    else "assistant",
                                    "content": str(msg),
                                }
                            ],
                            user_id=str(msg.user_id)
                            if isinstance(msg, MessageEvent)
                            else tentacle.profile.user_id,
                            run_id=rid,
                        )
                    )
                    for msg in messages
                    if str(msg)
                )
            )
        except Exception:
            logger.warning("Memory memo failed", exc_info=True)
