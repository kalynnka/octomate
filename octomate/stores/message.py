from __future__ import annotations

from octomate.database import async_session
from octomate.schemas.session import SessionKey
from octomate.transmuters.messages import Message


class MessageStore:
    async def save_all(self, records: list[Message]) -> None:
        if not records:
            return
        async with async_session() as session:
            session.add_all(records)
            await session.commit()

    async def find_by_ids(self, message_ids: list[str]) -> dict[str, Message]:
        if not message_ids:
            return {}
        async with async_session() as session:
            results = await session.list(
                Message,
                expressions=[Message["message_id"].in_(message_ids)],
            )
            return {m.message_id: m for m in results}

    async def list_recent(
        self,
        key: SessionKey,
        size: int | None = None,
    ) -> list[Message]:
        expressions = [
            Message["tentacle_id"] == key.tentacle_id,
            Message["chat"] == (key.group_id or key.user_id),
        ]
        if key.thread_id:
            expressions.append(Message["thread_id"] == key.thread_id)
        else:
            expressions.append(Message["thread_id"].is_(None))

        async with async_session() as session:
            return list(
                await session.list(
                    Message,
                    order_bys=[Message["id"].desc()],
                    limit=size,
                )
            )

    async def search(
        self,
        key: SessionKey,
        query: str = "",
        offset: int = 0,
        limit: int = 20,
    ) -> list[Message]:
        chat = key.group_id or key.user_id
        limit = max(1, min(limit, 50))
        offset = max(0, offset)

        expressions = [
            Message["tentacle_id"] == key.tentacle_id,
            Message["chat"] == chat,
        ]

        if key.thread_id:
            expressions.append(Message["thread_id"] == key.thread_id)
        else:
            expressions.append(Message["thread_id"].is_(None))

        if query:
            expressions.append(Message["content"].contains(query))

        async with async_session() as session:
            return list(
                await session.list(
                    Message,
                    expressions=expressions,
                    order_bys=[Message["timestamp"].desc(), Message["id"].desc()],
                    limit=limit,
                    offset=offset,
                )
            )
