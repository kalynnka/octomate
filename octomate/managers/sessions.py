from __future__ import annotations

from collections import OrderedDict
from collections.abc import Sequence
from typing import ClassVar

from octomate.database import async_session
from octomate.schemas.messages import ModelMessage
from octomate.schemas.session import Session, SessionKey


class SessionManager:
    """Manages `Session` entities with a SessionKey-keyed in-memory cache.

    Messages are NOT cached separately — `Session.messages` is loaded eagerly
    by arcanus' relationship (lazy='selectin' on the ORM), so a cached Session
    carries its full message history with it. `append_messages` evicts the
    cache entry so the next read reloads with the new messages attached.
    """

    DEFAULT_CACHE_SIZE: ClassVar[int] = 128

    cache: OrderedDict[SessionKey, Session]
    cache_size: int

    def __init__(self, *, cache_size: int | None = None) -> None:
        self.cache = OrderedDict()
        self.cache_size = cache_size or self.DEFAULT_CACHE_SIZE

    async def ensure(
        self,
        session_key: SessionKey,
        *,
        agent_tentacle_id: str | None = None,
    ) -> Session:
        if (hit := self._cache_get(session_key)) is not None:
            return hit

        async with async_session() as db_session:
            existing = await db_session.one_or_none(
                Session,
                expressions=[
                    Session["channel_tentacle_id"] == session_key.channel_tentacle_id,
                    Session["chat_type"] == session_key.chat_type,
                    Session["chat_id"] == session_key.chat_id,
                    Session["user_id"] == session_key.user_id,
                    Session["thread_id"] == session_key.thread_id,
                ],
            )
            if existing is not None:
                # Materialize messages while the session is still open so
                # callers can read them after the DB session closes (the ORM
                # would otherwise raise DetachedInstanceError on lazy access).
                await existing.messages
                self._cache_put(session_key, existing)
                return existing

            created = Session(
                channel_tentacle_id=session_key.channel_tentacle_id,
                chat_type=session_key.chat_type,
                chat_id=session_key.chat_id,
                user_id=session_key.user_id,
                thread_id=session_key.thread_id,
                agent_tentacle_id=agent_tentacle_id,
            )
            db_session.add(created)
            await db_session.commit()
            await created.messages

        self._cache_put(session_key, created)
        return created

    async def append_messages(
        self,
        session: Session,
        messages: Sequence[ModelMessage],
    ) -> None:
        if not messages:
            return
        async with async_session() as db_session:
            for msg in messages:
                msg.session_id = session.id
                db_session.add(msg)
            await db_session.commit()
        # The cached session's `.messages` is stale once we've inserted more
        # rows pointing at its id — drop it so the next read reloads fresh.
        self._cache_evict(session.key)

    def _cache_get(self, key: SessionKey) -> Session | None:
        if key not in self.cache:
            return None
        self.cache.move_to_end(key)
        return self.cache[key]

    def _cache_put(self, key: SessionKey, value: Session) -> None:
        if key in self.cache:
            self.cache.move_to_end(key)
            self.cache[key] = value
            return
        self.cache[key] = value
        if len(self.cache) > self.cache_size:
            self.cache.popitem(last=False)

    def _cache_evict(self, key: SessionKey) -> None:
        self.cache.pop(key, None)
