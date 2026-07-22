from functools import cache

from arcanus.materia.sqlalchemy import AsyncSession
from pydantic import JsonValue
from pydantic_core import from_json, to_json
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine

from octomate.config.database import database_settings


def json_serializer(value: JsonValue) -> str:
    """Serialize JSON columns via pydantic, tolerating Pydantic-friendly values.

    Arcanus/Pydantic schemas own validation; the engine only prepares values for
    the database JSON serializer.
    """
    return to_json(value, bytes_mode="base64", fallback=str).decode()


def json_deserializer(value: str | bytes) -> JsonValue:
    return from_json(value)


@cache
def engine() -> AsyncEngine:
    return create_async_engine(
        database_settings.db_url,
        json_serializer=json_serializer,
        json_deserializer=json_deserializer,
    )


@cache
def session_maker() -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine(), class_=AsyncSession, expire_on_commit=False)


def async_session() -> AsyncSession:
    return session_maker()()
