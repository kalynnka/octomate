from __future__ import annotations

import logging
from functools import cache
from pathlib import Path

from arcanus.materia.sqlalchemy import AsyncSession
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine

logger = logging.getLogger(__name__)

DB_PATH = Path(".octomate/octomate.db")
DB_URL = f"sqlite+aiosqlite:///{DB_PATH}"


@cache
def engine(db_path: Path = DB_PATH) -> AsyncEngine:
    return create_async_engine(DB_URL)


@cache
def session_maker() -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine(), class_=AsyncSession, expire_on_commit=False)


def async_session() -> AsyncSession:
    return session_maker()()
