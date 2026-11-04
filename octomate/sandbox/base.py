"""The runtime interface consumed by the sandbox manager."""

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
        """Create or reuse the runtime, raising SandboxError on failure."""
        ...

    async def open(self) -> ByteStream:
        """Start the agent and return its duplex stream, which the caller closes."""
        ...

    async def stop(self) -> None:
        """Stop the runtime and agent attachment, preserving persistent data."""
        ...
