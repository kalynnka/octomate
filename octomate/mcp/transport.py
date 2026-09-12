from __future__ import annotations

import asyncio
import socket
import ssl
from collections.abc import Iterable

import httpx2
from httpcore2 import AsyncConnectionPool, AsyncNetworkStream, ConnectError
from httpcore2._backends.anyio import AnyIOBackend
from httpcore2._backends.base import SOCKET_OPTION
from pydantic import IPvAnyAddress, TypeAdapter

IP_ADDRESSES = TypeAdapter(list[IPvAnyAddress])


class PublicMcpNetwork(AnyIOBackend):
    """Resolve and pin each connection to a public address, including reconnects."""

    async def connect_tcp(
        self,
        host: str,
        port: int,
        # httpcore's backend contract requires this parameter.
        timeout: float | None = None,  # noqa: ASYNC109
        local_address: str | None = None,
        socket_options: Iterable[SOCKET_OPTION] | None = None,
    ) -> AsyncNetworkStream:
        async with asyncio.timeout(timeout):
            addresses = await asyncio.get_running_loop().getaddrinfo(
                host=host, port=port, type=socket.SOCK_STREAM
            )
            ips = IP_ADDRESSES.validate_python([address[4][0] for address in addresses])
            if not ips or any(not ip.is_global or ip.is_multicast for ip in ips):
                raise ConnectError(
                    "MCP endpoints must resolve only to public addresses"
                )
            # Pass the checked numeric address to the socket layer; TLS still uses
            # the original hostname held by httpcore's connection.
            return await super().connect_tcp(
                host=str(addresses[0][4][0]),
                port=port,
                timeout=timeout,
                local_address=local_address,
                socket_options=socket_options,
            )


class PublicMcpTransport(httpx2.AsyncHTTPTransport):
    def __init__(self) -> None:
        # HTTPX exposes no network-backend argument. Keep its request/response
        # adapter and supply the httpcore pool at this one dependency boundary.
        self._pool = AsyncConnectionPool(
            ssl_context=ssl.create_default_context(),
            network_backend=PublicMcpNetwork(),
        )

    async def handle_async_request(self, request: httpx2.Request) -> httpx2.Response:
        response = await super().handle_async_request(request)
        if response.has_redirect_location:
            await response.aclose()
            raise httpx2.HTTPStatusError(
                "Install the final MCP endpoint URL; redirects are not allowed",
                request=request,
                response=response,
            )
        return response


def mcp_http_client(
    headers: dict[str, str] | None = None,
    timeout: httpx2.Timeout | None = None,
    auth: httpx2.Auth | None = None,
    follow_redirects: bool = False,
) -> httpx2.AsyncClient:
    return httpx2.AsyncClient(
        headers=headers,
        timeout=timeout or httpx2.Timeout(30),
        auth=auth,
        transport=PublicMcpTransport(),
        trust_env=False,
        # A redirect must never carry an instance's credential to another
        # endpoint. Users install the final MCP URL explicitly.
        follow_redirects=False,
    )
