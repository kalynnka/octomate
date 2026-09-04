from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from weakref import WeakValueDictionary


class SessionLocks:
    """Per-session `asyncio.Lock`s. Not a lock of its own — a thin registry over the
    stdlib lock, adding the keying the stdlib has no primitive for, plus self-cleanup:
    each entry is a weak reference, so once no task holds or waits on a key's lock it is
    reclaimed and the key leaves nothing behind.

    `async with locks.hold(key)` releases on every exit — return, exception, or
    cancellation — so an acquire cancelled by its timeout frees the lock automatically,
    no manual dismissal or force-release.
    """

    def __init__(self) -> None:
        self.by_session: WeakValueDictionary[str, asyncio.Lock] = WeakValueDictionary()

    @asynccontextmanager
    async def hold(
        self,
        key: str,
        *,
        acquire_timeout: float | None = None,
    ) -> AsyncGenerator[None]:
        """Hold the key's lock for the block. `acquire_timeout` bounds the wait for the
        lock and nothing else: a waiter stuck behind a wedged holder raises
        `TimeoutError` rather than blocking forever, while the block it guards runs
        unbounded once entered. A caller wanting to bound the work too wraps the whole
        `async with` in its own cancel scope; a never-acquired lock is never released."""
        # The local `lock` is a strong reference held for the whole block, so the weak
        # entry stays alive while anyone holds or waits on it — concurrent users share
        # the one live lock — and is reclaimed once the last user leaves.
        lock = self.by_session.get(key)
        if lock is None:
            lock = asyncio.Lock()
            self.by_session[key] = lock
        await asyncio.wait_for(lock.acquire(), acquire_timeout)
        try:
            yield
        finally:
            lock.release()
