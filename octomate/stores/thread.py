from __future__ import annotations

from collections import OrderedDict
from datetime import datetime
from typing import TYPE_CHECKING, ClassVar

from sqlalchemy.dialects.postgresql import insert as pg_insert
from uuid_utils import uuid7

from octomate.database import async_session
from octomate.schemas.session import SessionKey
from octomate.transmuters.threads import Thread

if TYPE_CHECKING:
    from octomate.tentacles.base import Tentacle


class ThreadStore:
    """Lazy thread store — looks up threads from DB on demand."""

    THREAD_CACHE_SIZE: ClassVar[int] = 512

    default_owner: str
    thread_cache: OrderedDict[SessionKey, Thread]

    def __init__(self, default_owner: str) -> None:
        self.default_owner = default_owner
        self.thread_cache = OrderedDict()

    def _get_cached_thread(self, key: SessionKey) -> Thread | None:
        thread = self.thread_cache.get(key)
        if thread is not None:
            self.thread_cache.move_to_end(key)
        return thread

    def _set_cached_thread(self, key: SessionKey, thread: Thread) -> None:
        if (
            key not in self.thread_cache
            and len(self.thread_cache) >= self.THREAD_CACHE_SIZE
        ):
            self.thread_cache.popitem(last=False)
        self.thread_cache[key] = thread
        self.thread_cache.move_to_end(key)

    async def get(self, key: SessionKey) -> Thread:
        cached = self._get_cached_thread(key)
        if cached is not None:
            return cached

        async with async_session() as session:
            expressions = [
                Thread["tentacle_id"] == key.tentacle_id,
                Thread["user_id"] == key.user_id,
                Thread["group_id"] == key.group_id,
                Thread["thread_id"] == key.thread_id,
                Thread["chat_id"] == key.chat_id,
            ]
            existing = await session.one_or_none(Thread, expressions=expressions)
            if existing:
                self._set_cached_thread(key, existing)
                return existing

            thread = Thread(
                tentacle_id=key.tentacle_id,
                user_id=key.user_id,
                group_id=key.group_id,
                thread_id=key.thread_id,
                chat_id=key.chat_id,
                owner_tentacle=self.default_owner,
            )
            session.add(thread)
            await session.commit()
            self._set_cached_thread(key, thread)
            return thread

    async def get_owner(self, key: SessionKey) -> str:
        thread = await self.get(key)
        return thread.owner_tentacle

    async def get_agent_session_id(self, key: SessionKey) -> str | None:
        """Return the persisted agent session_id for this key, or None."""
        thread = await self.get(key)
        return thread.agent_session_id

    async def get_session_name(self, key: SessionKey) -> str | None:
        """Return the persisted session_name for this key, or None."""
        thread = await self.get(key)
        return thread.session_name

    async def set_agent_session_id(self, key: SessionKey, session_id: str) -> None:
        """Persist the agent session_id for this key so it survives restarts."""
        async with async_session() as session:
            stmt = (
                pg_insert(Thread)
                .values(
                    id=str(uuid7()),
                    tentacle_id=key.tentacle_id,
                    user_id=key.user_id,
                    group_id=key.group_id,
                    thread_id=key.thread_id,
                    chat_id=key.chat_id,
                    owner_tentacle=self.default_owner,
                    agent_session_id=session_id,
                    created_at=datetime.now(),
                )
                .on_conflict_do_update(
                    constraint="uq_threads_session_key",
                    set_={"agent_session_id": session_id},
                )
                .returning(Thread)
            )
            thread = (await session.execute(stmt)).scalar_one()
            await session.commit()
        self._set_cached_thread(key, thread)

    async def set_session_name(self, key: SessionKey, session_name: str) -> None:
        """Persist the session_name for this key so it survives restarts."""
        async with async_session() as session:
            stmt = (
                pg_insert(Thread)
                .values(
                    id=str(uuid7()),
                    tentacle_id=key.tentacle_id,
                    user_id=key.user_id,
                    group_id=key.group_id,
                    thread_id=key.thread_id,
                    chat_id=key.chat_id,
                    owner_tentacle=self.default_owner,
                    session_name=session_name,
                    created_at=datetime.now(),
                )
                .on_conflict_do_update(
                    constraint="uq_threads_session_key",
                    set_={"session_name": session_name},
                )
                .returning(Thread)
            )
            thread = (await session.execute(stmt)).scalar_one()
            await session.commit()
        self._set_cached_thread(key, thread)

    async def get_worktree_info(self, key: SessionKey) -> tuple[str, str] | None:
        """Return (worktree_path, branch_name) for this key, or None if not set."""
        thread = await self.get(key)
        if thread.worktree_path and thread.branch_name:
            return (thread.worktree_path, thread.branch_name)
        return None

    async def set_worktree_info(
        self, key: SessionKey, worktree_path: str, branch_name: str
    ) -> None:
        """Persist worktree_path and branch_name for this key so they survive restarts."""
        async with async_session() as session:
            stmt = (
                pg_insert(Thread)
                .values(
                    id=str(uuid7()),
                    tentacle_id=key.tentacle_id,
                    user_id=key.user_id,
                    group_id=key.group_id,
                    thread_id=key.thread_id,
                    chat_id=key.chat_id,
                    owner_tentacle=self.default_owner,
                    worktree_path=worktree_path,
                    branch_name=branch_name,
                    created_at=datetime.now(),
                )
                .on_conflict_do_update(
                    constraint="uq_threads_session_key",
                    set_={
                        "worktree_path": worktree_path,
                        "branch_name": branch_name,
                    },
                )
                .returning(Thread)
            )
            thread = (await session.execute(stmt)).scalar_one()
            await session.commit()
        self._set_cached_thread(key, thread)

    async def set_owner(self, key: SessionKey, owner: Tentacle) -> None:
        old = self.thread_cache.pop(key, None)
        async with async_session() as session:
            stmt = (
                pg_insert(Thread)
                .values(
                    id=str(uuid7()),
                    tentacle_id=key.tentacle_id,
                    user_id=key.user_id,
                    group_id=key.group_id,
                    thread_id=key.thread_id,
                    chat_id=key.chat_id,
                    owner_tentacle=owner.id,
                    created_at=datetime.now(),
                )
                .on_conflict_do_update(
                    constraint="uq_threads_session_key",
                    set_={"owner_tentacle": owner.id},
                )
                .returning(Thread)
            )
            thread = (await session.execute(stmt)).scalar_one()
            await session.commit()
        if old is not None:
            self._set_cached_thread(key, thread)
