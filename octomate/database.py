from __future__ import annotations

import logging
from functools import cache
from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

logger = logging.getLogger(__name__)

DB_PATH = Path(".octomate/octomate.db")
DB_URL = f"sqlite+aiosqlite:///{DB_PATH}"


@cache
def engine(db_path: Path = DB_PATH) -> AsyncEngine:
    return create_async_engine(DB_URL)
