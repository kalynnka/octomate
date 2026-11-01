from __future__ import annotations

import asyncio
import os
import sys
from contextlib import AsyncExitStack
from pathlib import Path

import anyio
import pytest
from uuid_utils.compat import uuid7

from octomate.fibers import StdioFiber
from octomate.nerve import (
    Impulse,
    Nerve,
    Publication,
    SandboxTopic,
    UserTopic,
)
from octomate.schemas.sandbox import (
    CancelInvocation,
    InvocationRejected,
    SandboxAgentReady,
)
from tests.test_sandbox_contracts import IMAGE

REPOSITORY = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("fault", [None, "invalid-frame", "foreign-topic", "ui-topic"])
async def test_bootstrap_uses_a_stdio_fiber_with_the_core_nerve(
    tmp_path: Path, fault: str | None
) -> None:
    owner, invocation = uuid7(), uuid7()
    environment = {
        "PATH": os.environ["PATH"],
        "PYTHONPATH": str(REPOSITORY),
        "OCTOMATE_HOME": str(tmp_path),
        "OCTOMATE_DB_URL": "sqlite+aiosqlite:///:memory:",
        "OCTOMATE_SANDBOX_USER_ID": str(owner),
    }
    environment.update(
        OCTOMATE_SANDBOX_IMAGE=IMAGE,
        OCTOMATE_SANDBOX_CPUS="2",
        OCTOMATE_SANDBOX_MEMORY_MIB="2048",
    )
    async with AsyncExitStack() as stack:
        process = await stack.enter_async_context(
            await anyio.open_process(
                [sys.executable, "-m", "octomate.sandbox.entrypoint"],
                env=environment,
            )
        )
        assert process.stdout is not None
        assert process.stdin is not None
        assert process.stderr is not None
        topic = SandboxTopic(owner)
        nerve = Nerve()
        fiber = StdioFiber(process.stdout, process.stdin, Publication.adapter(topic))
        impulses = await stack.enter_async_context(nerve.subscribe(topic))
        stack.push_async_callback(fiber.aclose)
        await stack.enter_async_context(nerve.subscribe(topic, fiber=fiber))
        await stack.enter_async_context(nerve.publish(topic, fiber=fiber))
        connection = fiber.start()
        async with asyncio.timeout(30):
            ready = (await impulses.receive()).event
            assert isinstance(ready, SandboxAgentReady)
            assert ready.user_id == owner
            assert not ready.accepts_invocations
            if fault == "invalid-frame":
                await process.stdin.send(
                    b'{"version":999,"secret":"must-not-be-echoed"}\n'
                )
            elif fault is not None:
                publication = Publication(
                    topic=SandboxTopic(uuid7())
                    if fault == "foreign-topic"
                    else UserTopic(owner),
                    impulse=Impulse(CancelInvocation(invocation_id=invocation)),
                )
                await process.stdin.send(publication.model_dump_json().encode() + b"\n")
            else:
                sent = Impulse(CancelInvocation(invocation_id=invocation))
                nerve.deliver(topic, sent)
                assert await impulses.receive() == sent
                rejected = (await impulses.receive()).event
                assert isinstance(rejected, InvocationRejected)
                assert rejected.invocation_id == invocation
                await process.stdin.aclose()
            await process.wait()
            await connection
            await fiber.wait_closed()
            error = b"".join([chunk async for chunk in process.stderr])
        if fault is not None:
            assert process.returncode != 0
            assert b"must-not-be-echoed" not in error
            assert b"Sandbox stdio fiber failed" in error
        else:
            assert process.returncode == 0, error.decode()
        nerve.deliver(topic, Impulse(ready))
        assert await impulses.receive() == Impulse(ready)
