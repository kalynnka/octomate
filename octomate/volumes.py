"""Host-directory backing and shared active-use accounting for logical volumes."""

from __future__ import annotations

import asyncio
import shutil
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path

import anyio


@dataclass
class HostDirectory:
    """One host directory, shared by all overlapping uses of that backing."""

    path: Path  # Absolute host path; habitat destinations are separate.
    lock: asyncio.Lock = field(default_factory=asyncio.Lock, init=False)
    uses: int = field(default=0, init=False)  # Uses currently retaining the contents.

    def __post_init__(self) -> None:
        self.path = self.path.absolute()

    async def claim(self, *, ephemeral: bool) -> None:
        """Retain this backing, clearing abandoned temporary contents first."""
        async with self.lock:
            if ephemeral and not self.uses and self.path.exists():
                await asyncio.to_thread(shutil.rmtree, self.path)
            self.uses += 1

    async def discard(self, *, ephemeral: bool) -> None:
        """Drop one use, deleting temporary contents only after the last use."""
        async with self.lock:
            if not self.uses:
                raise RuntimeError("The host directory has no active use to release")
            self.uses -= 1
            if ephemeral and not self.uses and self.path.is_dir():
                await asyncio.to_thread(shutil.rmtree, self.path)

    @asynccontextmanager
    async def use(self, *, ephemeral: bool) -> AsyncGenerator[Path]:
        """Balance a claim through preparation failures, exceptions and cancellation."""
        await self.claim(ephemeral=ephemeral)
        try:
            yield self.path
        finally:
            with anyio.CancelScope(shield=True):
                await self.discard(ephemeral=ephemeral)

    async def release(self) -> None:
        """Remove inactive contents without changing a logical volume's identity."""
        async with self.lock:
            if self.uses:
                raise RuntimeError("Cannot release a host directory while it is in use")
            if self.path.is_dir():
                await asyncio.to_thread(shutil.rmtree, self.path)
