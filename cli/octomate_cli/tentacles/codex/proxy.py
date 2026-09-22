"""Connect the Codex plugin's stdio MCP transport to the configured Octomate."""

from __future__ import annotations

import anyio
import httpx2
from mcp.client.streamable_http import streamable_http_client
from mcp.server.stdio import stdio_server

from octomate_cli.tentacles.mcp import (
    CLIENT_HEADER,
    CODEX_NATIVE_CLIENT,
    octomate_secret,
    octomate_url,
)


async def proxy() -> None:
    url = octomate_url(None)
    headers = {
        "Authorization": f"Bearer {octomate_secret()}",
        CLIENT_HEADER: CODEX_NATIVE_CLIENT,
    }
    async with (
        httpx2.AsyncClient(
            headers=headers, timeout=httpx2.Timeout(30, read=300)
        ) as client,
        streamable_http_client(url, http_client=client) as (remote_read, remote_write),
        stdio_server() as (local_read, local_write),
        local_read,
        local_write,
        anyio.create_task_group() as tasks,
    ):

        async def receive() -> None:
            async for message in remote_read:
                if isinstance(message, Exception):
                    raise message
                await local_write.send(message)
            tasks.cancel_scope.cancel()

        tasks.start_soon(receive)
        async for message in local_read:
            if isinstance(message, Exception):
                raise message
            await remote_write.send(message)
        tasks.cancel_scope.cancel()


def main() -> None:
    anyio.run(proxy)


if __name__ == "__main__":
    main()
