from __future__ import annotations

import logging
from functools import cache

from arcanus.materia.sqlalchemy import AsyncSession
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine

from octomate.config import OctomateConfig

logger = logging.getLogger(__name__)


@cache
def engine() -> AsyncEngine:
    return create_async_engine(OctomateConfig().db_url)


@cache
def session_maker() -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine(), class_=AsyncSession, expire_on_commit=False)


def async_session() -> AsyncSession:
    return session_maker()()
