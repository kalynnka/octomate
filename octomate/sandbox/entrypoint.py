"""Sandbox image entrypoint: exchange signals with the core over a stdio fiber."""

import asyncio
import os
import sys
from contextlib import aclosing
from uuid import UUID

import anyio
from anyio.streams.file import FileReadStream, FileWriteStream

from octomate.fibers import FiberError, StdioFiber
from octomate.nerve import Impulse, NerveEvent, impulse_adapter
from octomate.schemas.sandbox import (
    CancelInvocation,
    ExecuteAgent,
    InvocationRejected,
    ProvideInput,
    SandboxAgentReady,
    ServiceRejected,
    ServiceReply,
)


async def reject_invocations(
    fiber: StdioFiber[Impulse[NerveEvent]],
) -> None:
    """Keep the bootstrap agent explicit about the missing React runner."""
    async for impulse in fiber.receive:
        if not isinstance(
            impulse.event,
            ExecuteAgent
            | ProvideInput
            | CancelInvocation
            | ServiceReply
            | ServiceRejected,
        ):
            continue
        await fiber.send.send(
            Impulse(
                InvocationRejected(
                    invocation_id=impulse.event.invocation_id,
                    reason="This sandbox agent provides bootstrap readiness only; agent runs are not enabled",
                ),
                invocation_id=impulse.event.invocation_id,
            ),
        )


async def main() -> None:
    ready = SandboxAgentReady(
        user_id=UUID(os.environ["OCTOMATE_SANDBOX_USER_ID"]),
        image=os.environ["OCTOMATE_SANDBOX_IMAGE"],
        cpus=int(os.environ["OCTOMATE_SANDBOX_CPUS"]),
        memory_mib=int(os.environ["OCTOMATE_SANDBOX_MEMORY_MIB"]),
        accepts_invocations=False,
    )
    fiber = StdioFiber(
        FileReadStream(os.fdopen(os.dup(sys.stdin.fileno()), "rb", buffering=0)),
        FileWriteStream(os.fdopen(os.dup(sys.stdout.fileno()), "wb", buffering=0)),
        impulse_adapter,
    )
    async with aclosing(fiber):
        fiber.start()
        try:
            await fiber.send.send(Impulse(ready))
            await reject_invocations(fiber)
        except (anyio.BrokenResourceError, anyio.ClosedResourceError):
            pass  # The core closed stdio; wait_closed reports any transport failure.
        await fiber.wait_closed()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except FiberError:
        raise SystemExit("Sandbox stdio fiber failed") from None
