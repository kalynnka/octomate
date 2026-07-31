from __future__ import annotations

import zlib
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from anyio import to_thread
from sqlalchemy import delete

from octomate.database import async_session
from octomate.schemas.spills import ToolOutputSpill

# zlib's default. What a spill actually holds — MCP JSON, logs, source — measures
# ~5x on JSON and ~20x on log text at this level, and the levels above it buy a few
# percent for several times the CPU.
COMPRESSION_LEVEL = 6


@dataclass
class SpillStore:
    """The tool-output-limits `OverflowStore`, backed by the Octomate database.

    The harness ships only a local-file store, whose handles resolve against one
    host's filesystem. A spill has to outlive the request that produced it — the
    handle is what the model reads back through `read_tool_result`, possibly turns
    later — and Octomate answers a conversation from whichever process picks the
    message up. Sharing the database is what makes those the same payload.

    Payloads are zlib-compressed. A spill is by definition the oversized end of what
    tools return, and that content is the compressible kind, so this is most of what
    keeps a blob column reasonable on SQLite.
    """

    # None keeps payloads forever, matching the harness's own store. A window is the
    # honest default here instead: a spill nobody read back within it is dead weight,
    # since the model has moved on and the tool can simply be called again.
    retention: timedelta | None = None

    async def write(self, key: str, data: bytes) -> str:
        """Persist `data` under `key`, returning it unchanged as the handle.

        The key already encodes the run, the tool call, and the retry, so it is
        self-contained — nothing else is needed to find the row again.
        """
        # Off the event loop: the work is proportional to a payload this store puts
        # no ceiling on, and a multi-megabyte return costs tens of milliseconds.
        packed = await to_thread.run_sync(zlib.compress, data, COMPRESSION_LEVEL)
        async with async_session() as session:
            session.add(ToolOutputSpill(handle=key, payload=packed))
            if self.retention is not None:
                # One statement rather than loading the expired rows to delete them
                # one by one: a transmuter always materializes whole, so listing them
                # would pull every stale payload through memory on the way to a DELETE.
                await session.execute(
                    delete(ToolOutputSpill).where(
                        ToolOutputSpill["created_at"]
                        < datetime.now(timezone.utc) - self.retention
                    )
                )
            await session.commit()
        return key

    async def read(self, handle: str) -> bytes:
        async with async_session() as session:
            spill = await session.one_or_none(
                ToolOutputSpill,
                expressions=[ToolOutputSpill["handle"] == handle],
            )
        if spill is None:
            # The protocol's own signal for an unknown handle: `read_tool_result`
            # catches OSError and answers the model with a correction rather than
            # spending a retry on it.
            raise FileNotFoundError(handle)
        return await to_thread.run_sync(zlib.decompress, spill.payload)
