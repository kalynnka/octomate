"""One retained workspace executor per habitat, with independent operation lifetimes."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from contextlib import suppress
from pathlib import Path
from typing import TYPE_CHECKING

import anyio
from anyio.abc import ByteStream
from anyio.streams.buffered import BufferedByteReceiveStream
from pydantic import UUID7
from pydantic_ai.workspaces import WorkspaceUnavailableError
from uuid_utils.compat import uuid7

from octomate.habitat.executor import (
    CancelledResponse,
    CancelRequest,
    CommandEvent,
    CommandOutput,
    CommandStarted,
    ExecuteRequest,
    ExecutionRequest,
    ExecutionResponse,
    Request,
    Response,
    execution_request_adapter,
)

if TYPE_CHECKING:
    from octomate.habitat.base import ManagedHabitat


class ExecutorClient:
    habitat: ManagedHabitat
    stream: ByteStream | None
    pending: dict[UUID7, asyncio.Future[Response | CancelledResponse]]
    listeners: dict[UUID7, Callable[[CommandEvent], None]]
    ready: asyncio.Future[None]
    writing: asyncio.Lock
    task: asyncio.Task[None]
    failure: Exception | None
    closed: bool

    def __init__(self, habitat: ManagedHabitat) -> None:
        self.habitat = habitat
        self.stream = None
        self.pending = {}
        self.listeners = {}
        self.ready = asyncio.get_running_loop().create_future()
        self.writing = asyncio.Lock()
        self.failure = None
        self.closed = False
        self.task = asyncio.create_task(self.run())

    async def run(self) -> None:
        try:
            source = await anyio.Path(
                Path(__file__).with_name("executor.py")
            ).read_text()
            async with self.habitat.resident(
                "python", "-c", source, workdir=str(self.habitat.root)
            ) as stream:
                self.stream = stream
                output = BufferedByteReceiveStream(stream)
                if await output.receive_until(b"\n", 64) != b"ready":
                    raise WorkspaceUnavailableError(
                        "Invalid executor readiness response"
                    )
                self.ready.set_result(None)
                while True:
                    size = int(await output.receive_until(b"\n", 32))
                    message = ExecutionResponse.model_validate_json(
                        await output.receive_exactly(size)
                    )
                    if isinstance(message.response, (CommandStarted, CommandOutput)):
                        self.listeners[message.id](message.response)
                    else:
                        self.pending[message.id].set_result(message.response)
        except Exception as error:
            self.failure = error
        finally:
            self.stream = None
            # Wake startup waiters even if startup failed; request() reports the failure.
            if not self.ready.done():
                self.ready.set_result(None)
            for future in self.pending.values():
                if not future.done():
                    future.set_exception(
                        WorkspaceUnavailableError("Habitat executor stopped")
                    )

    async def send(self, message: ExecutionRequest) -> None:
        if self.stream is None:
            raise WorkspaceUnavailableError(
                "Habitat executor is not running"
            ) from self.failure
        payload = execution_request_adapter.dump_json(message)
        async with self.writing:
            await self.stream.send(str(len(payload)).encode() + b"\n" + payload)

    async def request(
        self,
        request: Request,
        workdir: str,
        on_event: Callable[[CommandEvent], None] | None = None,
    ) -> Response:
        await asyncio.shield(self.ready)
        if self.failure is not None or self.closed:
            raise WorkspaceUnavailableError(
                "Habitat executor is unavailable"
            ) from self.failure
        key = uuid7()
        future: asyncio.Future[Response | CancelledResponse] = (
            asyncio.get_running_loop().create_future()
        )
        self.pending[key] = future
        if on_event is not None:
            self.listeners[key] = on_event
        sending = asyncio.create_task(
            self.send(
                ExecuteRequest(
                    id=key,
                    workdir=workdir,
                    request=request,
                    stream_output=on_event is not None,
                )
            )
        )
        try:
            await asyncio.shield(sending)
            response = await asyncio.shield(future)
            if isinstance(response, CancelledResponse):
                raise WorkspaceUnavailableError("Executor cancelled the operation")
            return response
        except asyncio.CancelledError:
            with anyio.CancelScope(shield=True):
                await sending
                if not future.done():
                    await self.send(CancelRequest(id=key))
                await future
            raise
        finally:
            self.pending.pop(key, None)
            self.listeners.pop(key, None)
            if future.done() and not future.cancelled():
                future.exception()

    async def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        with anyio.CancelScope(shield=True):
            with anyio.move_on_after(10):
                await asyncio.shield(self.ready)
                if self.stream is not None:
                    with suppress(anyio.BrokenResourceError, OSError):
                        await self.stream.send_eof()
                await asyncio.shield(self.task)
            if not self.task.done():
                self.task.cancel()
                await asyncio.gather(self.task, return_exceptions=True)
