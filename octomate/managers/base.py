import asyncio
from collections.abc import Hashable
from functools import cached_property


class Locks[Key: Hashable]:
    """Locks owned by this manager, shared by operations using the same key."""

    @cached_property
    def locks(self) -> dict[Key, asyncio.Lock]:
        return {}

    def lock(self, key: Key) -> asyncio.Lock:
        return self.locks.setdefault(key, asyncio.Lock())


class Manager:
    """Base for the application's managers."""
