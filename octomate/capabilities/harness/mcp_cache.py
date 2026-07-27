from __future__ import annotations

import asyncio
import logging
import uuid
from collections import OrderedDict
from collections.abc import Callable
from contextlib import AsyncExitStack
from dataclasses import dataclass, field

from pydantic_ai.mcp import MCPToolset
from pydantic_ai.toolsets import AbstractToolset

logger = logging.getLogger(__name__)


@dataclass
class McpCacheEntry:
    # The credential identity this session was built for; a change rebuilds it. Kept
    # out of repr so a token never lands in a log or traceback.
    fingerprint: str = field(repr=False)
    toolset: AbstractToolset[None]
    # Seconds this session's owning integration allows for warming — its own override
    # or the general default, resolved by the caller.
    warm_timeout: float
    exit_stack: AsyncExitStack = field(default_factory=AsyncExitStack, repr=False)
    warmed: bool = False


class McpToolsetCache:
    """Bounded, warm per-user MCP toolset cache shared across agent runs.

    A user's authenticated MCP session must never serve another user or another
    credential: swapping the bearer token would race concurrent runs, and the cached
    ``tools/list`` can differ by granted scope. So every ``(kind, key)`` — keyed by the
    durable user id — gets its own toolset and its own warm session.

    Each kind keeps an independent LRU bounded by the ``max_entries`` its caller
    supplies, and each entry warms under the ``warm_timeout`` its caller supplies — both
    the owning integration's own settings. Admitting one past the bound evicts and closes
    the least-recently-used session for that kind, and a changed fingerprint closes the
    stale session before rebuilding. Entering the cache keeps every live session warm for
    its lifetime; exiting closes them all.
    """

    def __init__(self) -> None:
        self.kinds: dict[str, OrderedDict[uuid.UUID, McpCacheEntry]] = {}
        self.lock = asyncio.Lock()
        self.active = False

    async def acquire(
        self,
        *,
        kind: str,
        key: uuid.UUID,
        fingerprint: str,
        max_entries: int,
        warm_timeout: float,
        build: Callable[[], AbstractToolset[None]],
    ) -> AbstractToolset[None]:
        """Return the warm toolset for ``(kind, key)``, building it if needed.

        Reuses the cached session when the fingerprint is unchanged; otherwise closes
        the stale one and builds a fresh session for the new credential. ``max_entries``
        bounds this kind's LRU and ``warm_timeout`` caps its warming — both the owning
        integration's own settings.
        """
        async with self.lock:
            entries = self.kinds.setdefault(kind, OrderedDict())
            entry = entries.get(key)
            if entry is not None and entry.fingerprint == fingerprint:
                entries.move_to_end(key)
            else:
                if entry is not None:
                    await entry.exit_stack.aclose()
                entry = McpCacheEntry(
                    fingerprint=fingerprint, toolset=build(), warm_timeout=warm_timeout
                )
                entries[key] = entry
                while len(entries) > max_entries:
                    _, evicted = entries.popitem(last=False)
                    await evicted.exit_stack.aclose()
            if self.active:
                await self.warm(entry)
            return entry.toolset

    async def warm(self, entry: McpCacheEntry) -> None:
        """Keep a session and its `tools/list` cache alive between agent runs.

        Holding one reference in the entry's own exit stack pins the session open; the
        run enters the same toolset again (reference-counted) and the priming call means
        the first run does not block on a multi-second `tools/list`.
        """
        if entry.warmed:
            return
        try:
            await asyncio.wait_for(
                entry.exit_stack.enter_async_context(entry.toolset),
                timeout=entry.warm_timeout,
            )
        except Exception:
            logger.warning(
                "Failed to warm MCP session; the agent run will retry",
                exc_info=True,
            )
            return
        entry.warmed = True

        mcp_servers: list[MCPToolset[None]] = []

        def collect(candidate: AbstractToolset[None]) -> None:
            if isinstance(candidate, MCPToolset):
                mcp_servers.append(candidate)

        entry.toolset.apply(collect)
        if mcp_servers:
            try:
                await asyncio.wait_for(
                    asyncio.gather(*(server.list_tools() for server in mcp_servers)),
                    timeout=entry.warm_timeout,
                )
            except Exception:
                logger.warning(
                    "Failed to prime MCP tools; the agent run will retry",
                    exc_info=True,
                )

    async def __aenter__(self) -> McpToolsetCache:
        self.active = True
        async with self.lock:
            for entries in self.kinds.values():
                for entry in entries.values():
                    await self.warm(entry)
        return self

    async def __aexit__(self, *exc: object) -> None:
        self.active = False
        async with self.lock:
            for entries in self.kinds.values():
                for entry in entries.values():
                    await entry.exit_stack.aclose()
            self.kinds.clear()
