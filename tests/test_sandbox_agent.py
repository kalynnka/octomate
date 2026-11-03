from __future__ import annotations

import asyncio
import os
import sys
from contextlib import AsyncExitStack
from pathlib import Path

import anyio
import pytest
from uuid_utils.compat import uuid7

from octomate.capabilities.harness.events import RunEndedEvent
from octomate.fibers import StdioFiber
from octomate.nerve import (
    Impulse,
    Nerve,
    SandboxTopic,
    impulse_adapter,
)
from octomate.schemas.sandbox import (
    SandboxAgentReady,
)
from tests.test_sandbox_contracts import IMAGE

REPOSITORY = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("unexpected_input", [False, True])
async def test_bootstrap_uses_a_stdio_fiber_with_the_core_nerve(
    tmp_path: Path, unexpected_input: bool
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
        topic = SandboxTopic(owner, invocation)
        nerve = Nerve()
        fiber = StdioFiber(impulse_adapter, input=process.stdout)
        impulses = await stack.enter_async_context(nerve.subscribe(topic))
        stack.push_async_callback(fiber.aclose)
        await stack.enter_async_context(nerve.publish(topic, fiber=fiber))
        connection = fiber.start()
        async with asyncio.timeout(30):
            ready = (await impulses.receive()).event
            assert isinstance(ready, SandboxAgentReady)
            assert ready.user_id == owner
            assert not ready.accepts_invocations
            local = Impulse(RunEndedEvent(), invocation_id=invocation)
            nerve.deliver(topic, local)
            assert await impulses.receive() == local
            if unexpected_input:
                await process.stdin.send(b"must-not-be-echoed\n")
            else:
                await process.stdin.aclose()
            await process.wait()
            await connection
            await fiber.wait_closed()
            error = b"".join([chunk async for chunk in process.stderr])
        if unexpected_input:
            assert process.returncode != 0
            assert b"must-not-be-echoed" not in error
            assert b"Sandbox stdio fiber failed" in error
        else:
            assert process.returncode == 0, error.decode()
        nerve.deliver(topic, Impulse(ready))
        assert await impulses.receive() == Impulse(ready)
