"""Carry ACP JSON-RPC over WebSocket text frames using the SDK's stream connection."""

import asyncio
import socket
from collections.abc import AsyncGenerator
from contextlib import AsyncExitStack, asynccontextmanager

from acp.connection import Connection
from acp.core import DEFAULT_STDIO_BUFFER_LIMIT_BYTES
from anyio import CancelScope
from fastapi import WebSocket
from pydantic import ValidationError
from pydantic_core import to_json

from octomate.tentacles.acp.adapter import json_value_adapter
from octomate.tentacles.acp.client import AcpClientHandler


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
