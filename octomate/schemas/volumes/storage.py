"""Volume storage contracts and the host-directory implementation."""

from __future__ import annotations

import asyncio
import shutil
from collections.abc import AsyncGenerator
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import field
from pathlib import Path
from typing import Literal, Protocol

import anyio
from pydantic import ConfigDict, Field, field_serializer
from pydantic.dataclasses import dataclass as pydantic_dataclass


class Storage(Protocol):
    """Storage exposed as a service-local directory for volume operations.

    Callers share one storage object per location so concurrent uses coordinate their
    lifetime. Habitat destination paths and access modes belong to habitats.
    """

    @property
    def path(self) -> Path:
        """Absolute service-local path, distinct from its habitat destination."""
        ...

    @property
    def lock(self) -> AbstractAsyncContextManager[None]:
        """Serialize changes to the contents and their active-use accounting."""
        ...

    @property
    def uses(self) -> int:
        """Number of active uses retaining the contents."""
        ...

    def acquire(self, *, ephemeral: bool) -> AbstractAsyncContextManager[Path]:
        """Retain contents until exit; reclaim temporary contents after last use."""
        ...

    async def release(self) -> None:
        """Remove inactive contents while the caller holds `lock`; refuse while in use."""
        ...


@pydantic_dataclass(config=ConfigDict(arbitrary_types_allowed=True))
class HostDirectory:
    """A host location, recorded relative to its storage root; runtime coordination
    is excluded from serialization."""

    path: Path  # Absolute once resolved; relative to the root as a row records it.
    kind: Literal["host-directory"] = field(default="host-directory", kw_only=True)
    root: Path | None = Field(
        default=None, exclude=True, kw_only=True
    )  # The storage root this directory is under; None outside it or unresolved.
    lock: asyncio.Lock = Field(default_factory=asyncio.Lock, exclude=True, init=False)
    uses: int = Field(
        default=0, exclude=True, init=False
    )  # Active uses; never persisted.

    def __post_init__(self) -> None:
        if ".." in self.path.parts:
            raise ValueError("Storage paths cannot contain parent traversal")

    @field_serializer("path")
    def record(self, path: Path) -> Path:
        """Record a directory under the root by its path beneath it, so the root
        can move without touching a row."""
        return path.relative_to(self.root) if self.root is not None else path

    async def claim(self, *, ephemeral: bool) -> None:
        """Retain this storage, clearing abandoned temporary contents first."""
        if not self.path.is_absolute():
            raise RuntimeError("A recorded host directory must be resolved before use")
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
    async def acquire(self, *, ephemeral: bool) -> AsyncGenerator[Path]:
        """Balance a claim through preparation failures, exceptions and cancellation."""
        await self.claim(ephemeral=ephemeral)
        try:
            yield self.path
        finally:
            with anyio.CancelScope(shield=True):
                await self.discard(ephemeral=ephemeral)

    async def release(self) -> None:
        """Remove inactive contents without changing a logical volume's identity.

        The caller holds `lock`, so whatever it checked first still holds and no
        use can begin until the contents are gone.
        """
        if not self.lock.locked():
            raise RuntimeError("Releasing a host directory requires its lock")
        if self.uses:
            raise RuntimeError("Cannot release a host directory while it is in use")
        if self.path.is_dir():
            await asyncio.to_thread(shutil.rmtree, self.path)
