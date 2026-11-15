import asyncio
import socket
from unittest.mock import AsyncMock

import httpx2
import pytest
from httpcore2 import AsyncNetworkStream, ConnectError
from httpcore2._backends.anyio import AnyIOBackend

from octomate.mcp.transport import PublicMcpNetwork, PublicMcpTransport, mcp_http_client


@pytest.mark.parametrize(
    "address",
    [
        "127.0.0.1",
        "10.1.2.3",
        "169.254.169.254",
        "192.168.1.1",
        "::1",
        "fc00::1",
        "::ffff:127.0.0.1",
        "0.0.0.0",
        "224.0.0.1",
    ],
)
async def test_connections_reject_private_destinations(
    monkeypatch: pytest.MonkeyPatch, address: str
) -> None:
    resolve = AsyncMock(
        return_value=[(socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, 443))]
    )
    connect = AsyncMock()
    monkeypatch.setattr(asyncio.get_running_loop(), "getaddrinfo", resolve)
    monkeypatch.setattr(AnyIOBackend, "connect_tcp", connect)
    with pytest.raises(ConnectError, match="public addresses"):
        await PublicMcpNetwork().connect_tcp(host="mcp.example", port=443)
    connect.assert_not_awaited()


async def test_dns_is_checked_again_and_socket_uses_checked_address(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    resolve = AsyncMock(
        side_effect=[
            [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, 443))]
            for address in ("8.8.8.8", "127.0.0.1")
        ]
    )
    stream = AsyncNetworkStream()
    connect = AsyncMock(return_value=stream)
    monkeypatch.setattr(asyncio.get_running_loop(), "getaddrinfo", resolve)
    monkeypatch.setattr(AnyIOBackend, "connect_tcp", connect)
    network = PublicMcpNetwork()
    assert await network.connect_tcp(host="mcp.example", port=443) is stream
    connect.assert_awaited_once_with(
        host="8.8.8.8",
        port=443,
        timeout=None,
        local_address=None,
        socket_options=None,
    )
    with pytest.raises(ConnectError, match="public addresses"):
        await network.connect_tcp(host="mcp.example", port=443)
    assert connect.await_count == 1


@pytest.mark.parametrize(
    "destination",
    ["https://other.example/mcp", "https://mcp.example/other", "http://127.0.0.1/mcp"],
)
async def test_transport_rejects_redirects_before_credentials_can_follow(
    monkeypatch: pytest.MonkeyPatch, destination: str
) -> None:
    upstream = AsyncMock(
        return_value=httpx2.Response(307, headers={"Location": destination})
    )
    monkeypatch.setattr(httpx2.AsyncHTTPTransport, "handle_async_request", upstream)
    async with mcp_http_client(
        headers={"Authorization": "Bearer secret"}, follow_redirects=True
    ) as client:
        assert client.follow_redirects is False
        with pytest.raises(httpx2.HTTPStatusError, match="redirects are not allowed"):
            await client.post("https://mcp.example/mcp")
    assert upstream.await_count == 1
    assert upstream.call_args.args[0].url.host == "mcp.example"


async def test_mcp_client_installs_the_guarded_transport() -> None:
    async with mcp_http_client() as client:
        assert isinstance(client._transport, PublicMcpTransport)
        assert client.trust_env is False
