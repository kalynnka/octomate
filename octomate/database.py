import os
from functools import cache

from arcanus.materia.sqlalchemy import AsyncSession
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine

DEFAULT_DB_URL = "sqlite+aiosqlite:///.octomate/octomate.db"


@cache
def engine() -> AsyncEngine:
    return create_async_engine(os.getenv("OCTOMATE_DB_URL", DEFAULT_DB_URL))


@cache
def session_maker() -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine(), class_=AsyncSession, expire_on_commit=False)


def async_session() -> AsyncSession:
    return session_maker()()
