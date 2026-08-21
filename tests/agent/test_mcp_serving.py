"""Octomate serving its MCP servers: each tool family at `/<name>/mcp`, every one
behind the hook secret, none of them without it.

Spoken to over the wire — through the mounted app, bearer and all — which is how a
driven Codex turn or a native session reaches them; the gateway's own tools are
pinned in memory by `test_gateway_tools`.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx
import pytest
from fastapi import FastAPI
from fastmcp import Client
from fastmcp.client.transports import StreamableHttpTransport
from fastmcp.exceptions import ToolError
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import AsyncEngine

from octomate.base import Octomate
from octomate.managers.gateway import GatewaySession
from octomate.mcp.gateway import CONVERSATION_HEADER, GATEWAY_SPELLS
from octomate.schemas.conversation import ChannelAddress
from tests.support.channels import FakeChannelTentacle

SECRET = SecretStr("the-hook-secret")
BEARER = {"Authorization": f"Bearer {SECRET.get_secret_value()}"}
SERVED = [server.name for server in Octomate().mcp_servers()]
LIST_TOOLS = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}


@pytest.fixture(autouse=True)
async def db(in_memory_engine: AsyncEngine) -> None:
    return


@asynccontextmanager
async def served() -> AsyncIterator[tuple[Octomate, FastAPI]]:
    """Octomate's app with its MCP servers up: their transports live in the app
    lifespan, which Starlette never runs for a mounted app on its own. A context
    rather than a fixture because the transport's task group must be left from
    the task that entered it, and a fixture's teardown runs in another."""
    octomate = Octomate(secret=SECRET)
    app = octomate.app()
    async with app.router.lifespan_context(app):
        yield octomate, app


def over(octomate: Octomate, app: FastAPI, headers: dict[str, str]) -> Client:
    """An MCP client speaking streamable HTTP into `app` without a socket."""

    def asgi(
        headers: dict[str, str] | None = None,
        timeout: httpx.Timeout | None = None,
        auth: httpx.Auth | None = None,
        follow_redirects: bool = True,
    ) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://octomate",
            headers=headers,
            timeout=timeout,
            auth=auth,
            follow_redirects=follow_redirects,
        )

    return Client(
        StreamableHttpTransport(
            f"http://octomate/gateway{octomate.mcp_path}",
            headers=headers,
            httpx_client_factory=asgi,
        )
    )


def a_driven_turn(octomate: Octomate) -> GatewaySession:
    """A turn at the gateway, as React registers one: the session a served call
    naming its conversation runs against."""
    session = GatewaySession(
        channel_routes={"im": []},
        current_agent_id="codex",
        channels={"im": FakeChannelTentacle()},
        conversation_id=uuid.uuid4(),
        conversation_address=ChannelAddress(
            channel_tentacle_id="im",
            chat_type="group",
            chat_id="room",
            user_id="alice",
            shared=True,
        ),
    )
    octomate.gateway.register(session)
    return session


def test_the_gateway_is_one_of_the_served_servers() -> None:
    assert "gateway" in SERVED


async def test_nothing_is_served_without_a_secret() -> None:
    octomate = Octomate()
    app = octomate.app()

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://octomate"
    ) as http:
        for name in SERVED:
            response = await http.post(f"/{name}{octomate.mcp_path}", json=LIST_TOOLS)
            assert response.status_code == 404


@pytest.mark.parametrize("name", SERVED)
@pytest.mark.parametrize(
    "headers",
    [
        pytest.param({}, id="no-header"),
        pytest.param({"Authorization": "Bearer wrong"}, id="wrong-secret"),
        pytest.param({"Authorization": "the-hook-secret"}, id="bare-secret-no-scheme"),
    ],
)
async def test_every_server_refuses_an_unauthenticated_call(
    name: str, headers: dict[str, str]
) -> None:
    async with (
        served() as (octomate, app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://octomate"
        ) as http,
    ):
        response = await http.post(
            f"/{name}{octomate.mcp_path}",
            json=LIST_TOOLS,
            headers={"Accept": "application/json, text/event-stream", **headers},
        )

    assert response.status_code == 401
    assert response.headers["www-authenticate"].startswith("Bearer")


async def test_the_secret_opens_the_gateway_and_its_five_spells() -> None:
    async with served() as (octomate, app), over(octomate, app, BEARER) as client:
        tools = await client.list_tools()

    assert [tool.name for tool in tools] == list(GATEWAY_SPELLS)


async def test_a_served_call_runs_against_the_turn_its_header_names() -> None:
    async with served() as (octomate, app):
        session = a_driven_turn(octomate)
        async with over(
            octomate, app, {**BEARER, CONVERSATION_HEADER: str(session.conversation_id)}
        ) as client:
            result = await client.call_tool("scry", {})

    assert result.data == str(await session.scry())


async def test_a_call_naming_no_turn_is_refused() -> None:
    async with served() as (octomate, app):
        a_driven_turn(octomate)

        async with over(octomate, app, BEARER) as client:
            with pytest.raises(ToolError, match="names no conversation"):
                await client.call_tool("scry", {})

        stray = {**BEARER, CONVERSATION_HEADER: "not-a-uuid"}
        async with over(octomate, app, stray) as client:
            with pytest.raises(ToolError, match="not a conversation id"):
                await client.call_tool("scry", {})

        unknown = {**BEARER, CONVERSATION_HEADER: str(uuid.uuid4())}
        async with over(octomate, app, unknown) as client:
            with pytest.raises(ToolError, match="No turn of conversation"):
                await client.call_tool("scry", {})
