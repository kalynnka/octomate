"""The runtime interface consumed by the sandbox manager."""

from contextlib import AbstractAsyncContextManager
from typing import Protocol

from anyio.abc import ByteStream


class SandboxError(RuntimeError):
    """An actionable failure safe to publish without guest or credential output."""


class Sandbox(Protocol):
    @property
    def name(self) -> str:
        """Stable identifier of this sandbox instance."""
        ...

    async def prepare(self) -> None:
        """Create or reuse, start, and validate the runtime before returning."""
        ...

    def execute(
        self, *command: str, workdir: str, env: dict[str, str] | None = None
    ) -> AbstractAsyncContextManager[ByteStream]:
        """Run a command with duplex stdio, closing and reaping it on exit."""
        ...

    async def stop(self) -> None:
        """Stop the runtime, preserving persistent data."""
        ...
