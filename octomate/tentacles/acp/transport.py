"""Carry ACP JSON-RPC through the SDK's stream connection."""

from __future__ import annotations

import asyncio
import socket
from collections.abc import AsyncGenerator
from contextlib import AsyncExitStack, asynccontextmanager
from typing import TYPE_CHECKING

import acp
import anyio
from acp.connection import Connection
from acp.core import DEFAULT_STDIO_BUFFER_LIMIT_BYTES
from anyio import CancelScope
from fastapi import WebSocket
from pydantic import ValidationError
from pydantic_core import to_json

from octomate.habitat import Habitat, ManagedHabitat
from octomate.tentacles.acp.adapter import json_value_adapter

if TYPE_CHECKING:
    from anyio.abc import ByteStream

    from octomate.tentacles.acp.client import AcpClientHandler


@asynccontextmanager
async def stdio_connection(
    handler: AcpClientHandler,
    command: str,
    *args: str,
    habitat: Habitat | None = None,
    cwd: str | None = None,
    env: dict[str, str] | None = None,
) -> AsyncGenerator[Connection]:
    """Own the provider process in its execution environment, preserving ACP bytes."""
    if not isinstance(habitat, ManagedHabitat):
        async with acp.spawn_stdio_connection(
            handler,
            command,
            *args,
            env=env,
            stderr=None,
            limit=DEFAULT_STDIO_BUFFER_LIMIT_BYTES,
        ) as (connection, _process):
            yield connection
        return
    if cwd is None:
        raise ValueError("Habitat ACP connections require a guest working directory")
    async with (
        habitat.resident(command, *args, workdir=cwd, env=env) as stream,
        stream_connection(stream, handler) as connection,
    ):
        yield connection


@asynccontextmanager
async def stream_connection(
    stream: ByteStream, handler: AcpClientHandler
) -> AsyncGenerator[Connection]:
    """Bridge a habitat byte stream to the SDK's public asyncio stream interface."""

    async def receive() -> None:
        try:
            while True:
                incoming.write(await stream.receive())
                await incoming.drain()
        except anyio.EndOfStream:
            incoming.write_eof()
        except Exception as error:
            reader.set_exception(error)

    async def send() -> None:
        try:
            while data := await outgoing.read(65536):
                await stream.send(data)
            await stream.send_eof()
        except Exception as error:
            reader.set_exception(error)

    local, peer = socket.socketpair()
    writers: list[asyncio.StreamWriter] = []
    receiving: asyncio.Task[None] | None = None
    sending: asyncio.Task[None] | None = None
    with local, peer:
        try:
            reader, writer = await asyncio.open_connection(
                sock=local, limit=DEFAULT_STDIO_BUFFER_LIMIT_BYTES
            )
            writers.append(writer)
            outgoing, incoming = await asyncio.open_connection(
                sock=peer, limit=DEFAULT_STDIO_BUFFER_LIMIT_BYTES
            )
            writers.append(incoming)
            receiving = asyncio.create_task(receive())
            sending = asyncio.create_task(send())
            async with Connection(handler, writer, reader) as connection:
                yield connection
        finally:
            with CancelScope(shield=True):
                for writer in writers:
                    writer.close()
                if sending is not None:
                    await sending
                if receiving is not None:
                    receiving.cancel()
                    await asyncio.gather(receiving, return_exceptions=True)
                await asyncio.gather(*(writer.wait_closed() for writer in writers))


@asynccontextmanager
async def websocket_connection(
    websocket: WebSocket, handler: AcpClientHandler
) -> AsyncGenerator[Connection]:
    """Adapt one JSON object per text frame to the SDK's newline-delimited streams.

    A local socket pair supplies real asyncio streams without a TCP listener or
    SDK-private hooks. The SDK owns request IDs, callbacks and dispatch. Call
    `main_loop` on the yielded connection to serve until the WebSocket closes.
    Malformed JSON, non-object payloads and binary frames close with code 1008.
    """
    stack = AsyncExitStack()
    try:
        sockets = socket.socketpair()
        for endpoint in sockets:
            stack.callback(endpoint.close)
        streams: list[tuple[asyncio.StreamReader, asyncio.StreamWriter]] = []
        for endpoint in sockets:
            reader, writer = await asyncio.open_connection(
                sock=endpoint, limit=DEFAULT_STDIO_BUFFER_LIMIT_BYTES
            )
            stack.push_async_callback(writer.wait_closed)
            stack.callback(writer.close)
            streams.append((reader, writer))
        (reader, writer), (outgoing, incoming) = streams
        connection = await stack.enter_async_context(
            Connection(handler, writer, reader, listening=False)
        )

        async def receive() -> None:
            try:
                while True:
                    message = await websocket.receive()
                    if message["type"] == "websocket.disconnect":
                        return
                    payload = json_value_adapter.validate_json(
                        message.get("text") or ""
                    )
                    if not isinstance(payload, dict) or payload.get("jsonrpc") != "2.0":
                        raise ValueError("Expected a JSON-RPC object")
                    incoming.write(to_json(payload) + b"\n")
                    await incoming.drain()
            except (ValidationError, ValueError):
                await websocket.close(
                    code=1008, reason="Expected a JSON-RPC text frame"
                )
            finally:
                incoming.write_eof()

        async def send() -> None:
            while line := await outgoing.readline():
                await websocket.send_text(line.decode().rstrip("\n"))

        async with asyncio.TaskGroup() as tasks:
            receiving = tasks.create_task(receive())
            sending = tasks.create_task(send())
            try:
                yield connection
            finally:
                receiving.cancel()
                sending.cancel()
    finally:
        with CancelScope(shield=True):
            await stack.aclose()
