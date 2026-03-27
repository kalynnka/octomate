from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from zep_cloud.client import AsyncZep
from zep_cloud.errors import NotFoundError
from zep_cloud.types import Message as ZepMessage

from octomate.memory.base import OctopusMemory
from octomate.schemas.actions import AgentMessage
from octomate.schemas.events import MessageEvent
from octomate.schemas.session import SessionKey, UserProfile

if TYPE_CHECKING:
    from octomate.tentacles.base import ChannelTentacle

logger = logging.getLogger(__name__)

BOT_USER_ID = "octomate"
MAX_ZEP_CONTENT_LENGTH = 10000
ZEP_BATCH_SIZE = 20


class ZepMemory(OctopusMemory):
    client: AsyncZep
    known_users: set[str]
    known_threads: set[str]

    def __init__(
        self,
        api_key: str,
        bot_name: str = "Octomate",
    ) -> None:
        self.client = AsyncZep(api_key=api_key)
        self.bot_name = bot_name
        self.known_users = set()
        self.known_threads = set()

    async def ensure_user(self, profile: UserProfile) -> None:
        if profile.user_id in self.known_users:
            return
        try:
            await self.client.user.get(profile.user_id)
        except NotFoundError:
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
        except NotFoundError:
            await self.client.thread.create(
                thread_id=thread_id,
                user_id=user_id,
            )
        self.known_threads.add(thread_id)

    @staticmethod
    def thread_id(key: SessionKey) -> str:
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
        if not events:
            return []

        try:
            await self.memo(key, events, tentacle)

            tid = self.thread_id(key)
            result = await self.client.thread.get_user_context(tid)
            if result.context:
                return [result.context]
        except Exception:
            logger.warning("Zep recall failed", exc_info=True)
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

        tid = self.thread_id(key)
        thread_owner_id = key.user_id if not key.group_id else tentacle.profile.user_id

        try:
            current_users: dict[str, UserProfile] = {}
            for msg in messages:
                if isinstance(msg, MessageEvent):
                    current_users[msg.user_id] = msg.sender

            await asyncio.gather(
                *(self.ensure_user(profile) for profile in current_users.values()),
                self.ensure_user(tentacle.profile),
            )
            await self.ensure_thread(tid, thread_owner_id)

            zep_messages = []
            for msg in messages:
                content = str(msg)
                if len(content) > MAX_ZEP_CONTENT_LENGTH:
                    content = content[:MAX_ZEP_CONTENT_LENGTH] + "…[truncated]"
                if isinstance(msg, MessageEvent):
                    zep_messages.append(
                        ZepMessage(
                            content=content,
                            role="user",
                            name=msg.display_name,
                        )
                    )
                else:
                    zep_messages.append(
                        ZepMessage(
                            content=content,
                            role="assistant",
                            name=tentacle.profile.name,
                        )
                    )

            for i in range(0, len(zep_messages), ZEP_BATCH_SIZE):
                batch = zep_messages[i : i + ZEP_BATCH_SIZE]
                await self.client.thread.add_messages(tid, messages=batch)
        except Exception:
            logger.warning("Zep memo failed", exc_info=True)
