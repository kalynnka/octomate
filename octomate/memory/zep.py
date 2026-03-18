from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import TYPE_CHECKING

from zep_cloud.client import AsyncZep
from zep_cloud.types import Message as ZepMessage

from octomate.memory.base import OctopusMemory
from octomate.schemas.actions import AgentMessage
from octomate.schemas.session import SessionKey, UserProfile

if TYPE_CHECKING:
    from octomate.schemas.events import MessageEvent
    from octomate.tentacles.base import Tentacle

logger = logging.getLogger(__name__)

BOT_USER_ID = "octomate"


class ZepMemory(OctopusMemory):
    client: AsyncZep
    known_users: set[str]
    known_threads: set[str]

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
        self.known_threads = set()

    async def ensure_user(self, profile: UserProfile) -> None:
        if profile.user_id in self.known_users:
            return
        try:
            await self.client.user.get(profile.user_id)
        except Exception:
            await self.client.user.add(
                user_id=profile.user_id,
                first_name=profile.name or profile.nickname or profile.user_id,
                last_name=None,
                email=getattr(profile, "email", None),
            )
        self.known_users.add(profile.user_id)

    async def ensure_thread(self, thread_id: str, user_id: str) -> None:
        if thread_id in self.known_threads:
            return
        try:
            await self.client.thread.get(thread_id)
        except Exception:
            await self.client.thread.create(
                thread_id=thread_id,
                user_id=user_id,
            )
        self.known_threads.add(thread_id)

    @staticmethod
    def thread_id(key: SessionKey) -> str:
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
        if not events:
            return []

        try:
            tid = self.thread_id(key)
            await self.ensure_thread(
                tid,
                tentacle.profile.user_id
                if key.group_id  # group chat uses tentacle user_id as thread owner
                else key.user_id,  # private chat uses user_id as thread owner
            )
            current_users = {event.user_id: event.sender for event in events}

            await asyncio.gather(
                *(
                    self.ensure_user(user_profile)
                    for user_profile in current_users.values()
                ),
                self.ensure_user(tentacle.profile),
                self.ensure_thread(tid, tentacle.profile.user_id),
            )

            messages = [
                ZepMessage(
                    content=str(event),
                    role="user",
                    name=event.display_name,
                )
                for event in events
            ]

            result = await self.client.thread.add_messages(
                tid,
                messages=messages,
                return_context=True,
            )
            if result.context:
                return [result.context]
        except Exception:
            logger.warning("Zep recall failed", exc_info=True)
        return []

    async def memo(
        self,
        key: SessionKey,
        messages: list[AgentMessage],
        tentacle: Tentacle,
    ) -> None:
        if not messages:
            return

        tid = self.thread_id(key)

        try:
            await asyncio.gather(
                self.ensure_user(tentacle.profile),
                self.ensure_thread(tid, tentacle.profile.user_id),
            )

            zep_messages = [
                ZepMessage(
                    content=str(msg),
                    role="assistant",
                    name=tentacle.profile.name,
                )
                for msg in messages
            ]
            if zep_messages:
                await self.client.thread.add_messages(tid, messages=zep_messages)
        except Exception:
            logger.warning("Zep memo failed", exc_info=True)
