"""SpillStore: the tool-output-limits overflow store over the Octomate database."""

from __future__ import annotations

import zlib
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from octomate.database import async_session
from octomate.managers.spills import COMPRESSION_LEVEL, SpillStore
from octomate.schemas.spills import ToolOutputSpill

PAYLOAD = b"x" * 50_000


async def seed(handle: str, payload: bytes, *, age: timedelta) -> None:
    """Write a spill of a chosen age, in the store's own on-disk format.

    `write` stamps `created_at` itself, so retention has to be seeded around it.
    """
    async with async_session() as session:
        session.add(
            ToolOutputSpill(
                handle=handle,
                payload=zlib.compress(payload, COMPRESSION_LEVEL),
                created_at=datetime.now(timezone.utc) - age,
            )
        )
        await session.commit()


async def test_write_then_read_round_trips(in_memory_engine: AsyncEngine) -> None:
    store = SpillStore()
    handle = await store.write("run-1/call-1.0", PAYLOAD)

    assert handle == "run-1/call-1.0"
    assert await store.read(handle) == PAYLOAD


async def test_read_of_unknown_handle_raises_file_not_found(
    in_memory_engine: AsyncEngine,
) -> None:
    """`read_tool_result` catches OSError to correct the model rather than
    spending a tool retry, so an unknown handle has to arrive as one."""

    with pytest.raises(FileNotFoundError):
        await SpillStore().read("run-1/never-written.0")


async def test_payload_is_stored_compressed(in_memory_engine: AsyncEngine) -> None:
    """The column holds the packed bytes, not the original — that reduction is
    what keeps a blob column tolerable on SQLite."""

    handle = await SpillStore().write("run-1/call-1.0", PAYLOAD)

    async with async_session() as session:
        stored = await session.one_or_none(
            ToolOutputSpill, expressions=[ToolOutputSpill["handle"] == handle]
        )
    assert stored is not None
    assert len(stored.payload) < len(PAYLOAD) / 10
    assert zlib.decompress(stored.payload) == PAYLOAD


async def test_binary_payload_survives(in_memory_engine: AsyncEngine) -> None:
    """The protocol is bytes in, bytes out — a tool may return an image."""

    store = SpillStore()
    payload = bytes(range(256)) * 100
    handle = await store.write("run-1/binary.0", payload)

    assert await store.read(handle) == payload


async def test_retention_prunes_only_what_it_has_outlived(
    in_memory_engine: AsyncEngine,
) -> None:
    store = SpillStore(retention=timedelta(hours=6))
    await seed("run-0/stale.0", b"stale", age=timedelta(hours=7))
    await seed("run-0/recent.0", b"recent", age=timedelta(hours=1))

    # Pruning rides the next write rather than a background sweep.
    await store.write("run-1/fresh.0", PAYLOAD)

    assert await store.read("run-0/recent.0") == b"recent"
    assert await store.read("run-1/fresh.0") == PAYLOAD
    with pytest.raises(FileNotFoundError):
        await store.read("run-0/stale.0")


async def test_retention_none_keeps_everything(in_memory_engine: AsyncEngine) -> None:
    """`None` matches the harness's own store: keep forever."""

    store = SpillStore()
    await seed("run-0/ancient.0", b"ancient", age=timedelta(days=365))

    await store.write("run-1/fresh.0", PAYLOAD)

    assert await store.read("run-0/ancient.0") == b"ancient"
