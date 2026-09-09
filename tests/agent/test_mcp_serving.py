"""Octomate serving its MCP server: one endpoint, `/octomate/mcp`, behind the
registered users' scoped API tokens, so a deployment with
no registered user serves it locked outright.

Spoken to over the wire — through the mounted app, bearer and all — which is how a
driven Codex turn or a native session reaches it; the gateway's own tools are
pinned in memory by `test_gateway_tools`.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Literal, Self

import httpx
import httpx2
import pytest
from fastapi import FastAPI
from fastmcp import Client
from fastmcp.client.transports import StreamableHttpTransport
from fastmcp.exceptions import ToolError
from sqlalchemy.ext.asyncio import AsyncEngine

from octomate.base import Octomate
from octomate.capabilities.history import HISTORY_TOOLS
from octomate.config.base import OctomateConfig
from octomate.config.channels import ChannelConfig
from octomate.managers.gateway import OctomateSession
from octomate.mcp.gateway import CLIENT_HEADER, CONVERSATION_HEADER, GATEWAY_SPELLS
from octomate.mcp.oauth import CONFIRM_TOOL, CONNECT_TOOL
from octomate.mcp.server import (
    CALL_MCP_TOOL,
    LIST_MCP_TOOLS,
    LIST_MCPS,
    OCTOMATE_MCP_PATH,
    gateway_tool,
    history_tool,
    octomate_instructions,
)
from octomate.schemas.awakes import GatewayHandoffSignal
from octomate.schemas.conversation import ChannelAddress
from octomate.schemas.triage import SummonDecision
from octomate.tentacles.mcp import OAuthMcpTentacle
from octomate.types.threads import CLAUDE_NATIVE_ID
from tests.support.agents import FakeAgent
from tests.support.channels import FakeChannelTentacle, FakeOctomate
from tests.support.users import a_api_key, a_user, auth_config

LIST_TOOLS = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}

# Octomate's own families, in the order the server lists them.
OCTOMATE_TOOLS = [*map(gateway_tool, GATEWAY_SPELLS), *map(history_tool, HISTORY_TOOLS)]


@pytest.fixture(autouse=True)
async def db(in_memory_engine: AsyncEngine) -> None:
    return


@asynccontextmanager
async def served(
    octomate: Octomate | None = None,
) -> AsyncIterator[tuple[Octomate, FastAPI]]:
    """Octomate's app with its MCP server up: the transport lives in the app
    lifespan, which Starlette never runs for a mounted app on its own. A context
    rather than a fixture because the transport's task group must be left from
    the task that entered it, and a fixture's teardown runs in another."""
    octomate = octomate or Octomate(config=OctomateConfig(auth=auth_config()))
    app = octomate
    async with app.router.lifespan_context(app):
        yield octomate, app


async def test_lifespan_prepares_agents_before_channels_and_serving() -> None:
    started: list[str] = []

    class DiscoveringAgent(FakeAgent):
        async def __aenter__(self) -> Self:
            self.models = {"discovered": "fake-model"}
            started.append("agent")
            return await super().__aenter__()

    class ReadyChannel(FakeChannelTentacle):
        async def probe(self) -> None:
            assert list(self.octomate.agents["inkling"].models) == ["discovered"]
            started.append("channel")
            await super().probe()

    octomate = Octomate()
    octomate.connect(ReadyChannel(octomate=octomate))
    octomate.connect(DiscoveringAgent(octomate=octomate, models={}))

    async with served(octomate):
        assert started == ["agent", "channel"]


def over(
    octomate: Octomate,
    app: FastAPI,
    headers: dict[str, str],
    *,
    mode: Literal["auto", "legacy", "2026-07-28"] = "auto",
) -> Client:
    """An MCP client speaking streamable HTTP into `app` without a socket."""

    def asgi(
        headers: dict[str, str] | None = None,
        timeout: httpx2.Timeout | None = None,
        auth: httpx2.Auth | None = None,
        follow_redirects: bool = True,
    ) -> httpx2.AsyncClient:
        return httpx2.AsyncClient(
            transport=httpx2.ASGITransport(app=app),
            base_url="http://octomate",
            headers=headers,
            timeout=timeout,
            auth=auth,
            follow_redirects=follow_redirects,
        )

    return Client(
        StreamableHttpTransport(
            f"http://octomate{OCTOMATE_MCP_PATH}",
            headers=headers,
            httpx_client_factory=asgi,
        ),
        mode=mode,
    )


DRIVEN_BEARER = {"Authorization": "Bearer lu-token"}


async def a_driven_deployment() -> Octomate:
    """A deployment whose driven turns carry the persisted bearer for `lu`."""
    user = await a_user("lu", profiles={"im": "alice"})
    await a_api_key(user, "lu-token")
    return Octomate(config=OctomateConfig(auth=auth_config()))


async def a_driven_turn(octomate: Octomate) -> OctomateSession:
    """A turn at the gateway, as React registers one: the session a served call
    naming its conversation runs against, kicked by `lu`'s linked account."""
    session = OctomateSession(
        channel_routes={"im": []},
        current_agent_id="codex",
        channels={"im": FakeChannelTentacle(octomate=octomate)},
        conversation_id=uuid.uuid4(),
        conversation_address=ChannelAddress(
            channel_tentacle_id="im",
            chat_type="group",
            chat_id="room",
            user_id="alice",
            shared=True,
        ),
        users=octomate.users,
        user_profile=await octomate.users.profile("im", "alice"),
    )
    octomate.gateway.register(session)
    return session


class ToolsTentacle(FakeChannelTentacle, OAuthMcpTentacle):
    """A channel composing the MCP component: a provider the link tools know by
    its id, acting as the person who linked it — nobody has, so it lists
    nothing."""

    label = "Tools"
    upstream = "https://tools.example/mcp"
    instructions = "## Tools\n\nA fake provider's own contract.\n"
    prefix = None


def test_every_tentacle_composing_mcp_is_a_provider_and_its_type_is_proxied_once() -> (
    None
):
    octomate = Octomate(config=OctomateConfig(auth=auth_config()))
    octomate.connect(ToolsTentacle(id="a", octomate=octomate))
    octomate.connect(ToolsTentacle(id="b", octomate=octomate))

    tentacles = list(octomate.mcps.values())
    instructions = octomate_instructions(tentacles)

    assert list(octomate.mcps) == ["a", "b"]
    assert f"`{CONNECT_TOOL}` with the provider's id (`a`, `b`)" in instructions
    assert "A fake provider's own contract." not in instructions
    assert "`a` (Tools), `b` (Tools)" in instructions
    assert instructions.count("## Linking accounts") == 1


def test_the_instructions_carry_the_linking_contract_only_with_a_provider() -> None:
    bare = octomate_instructions([])
    with_tools = octomate_instructions([ToolsTentacle(id="a")])

    assert "## Linking accounts" not in bare
    assert "## Linking accounts" in with_tools
    assert "Tools — act as the person" in with_tools
    assert "A fake provider's own contract." not in with_tools


async def test_a_provider_adds_the_link_tools_and_lists_nothing_of_its_own() -> None:
    # Initial discovery exposes local helpers even before an account is linked.
    octomate = await a_driven_deployment()
    octomate.connect(ToolsTentacle(id="a", octomate=octomate))
    async with served(octomate) as (octomate, app):
        session = await a_driven_turn(octomate)
        async with over(
            octomate,
            app,
            {**DRIVEN_BEARER, CONVERSATION_HEADER: str(session.conversation_id)},
        ) as client:
            tools = await client.list_tools()
            with pytest.raises(ToolError, match="No provider with id 'nope'"):
                await client.call_tool(CONNECT_TOOL, {"provider": "nope"})

    assert [tool.name for tool in tools] == [
        *OCTOMATE_TOOLS,
        LIST_MCPS,
        LIST_MCP_TOOLS,
        CALL_MCP_TOOL,
        CONNECT_TOOL,
        CONFIRM_TOOL,
    ]


async def test_a_bare_deployment_is_served_locked() -> None:
    # No registered user: the endpoint still exists, and nothing at all opens
    # it — every bearer there is names a person, and none is registered.
    async with served() as (_, app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://octomate"
        ) as http:
            response = await http.post(
                OCTOMATE_MCP_PATH,
                json=LIST_TOOLS,
                headers={"Accept": "application/json, text/event-stream"},
            )
            assert response.status_code == 401


@pytest.mark.parametrize(
    "headers",
    [
        pytest.param({}, id="no-header"),
        pytest.param({"Authorization": "Bearer wrong"}, id="wrong-secret"),
        pytest.param({"Authorization": "the-hook-secret"}, id="bare-secret-no-scheme"),
    ],
)
async def test_the_server_refuses_an_unauthenticated_call(
    headers: dict[str, str],
) -> None:
    async with (
        served() as (_, app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://octomate"
        ) as http,
    ):
        response = await http.post(
            OCTOMATE_MCP_PATH,
            json=LIST_TOOLS,
            headers={"Accept": "application/json, text/event-stream", **headers},
        )

    assert response.status_code == 401
    assert response.headers["www-authenticate"].startswith("Bearer")


@pytest.mark.parametrize("mode", ["legacy", "2026-07-28"])
async def test_an_api_token_opens_the_six_spells_and_the_history_tools(
    mode: Literal["legacy", "2026-07-28"],
) -> None:
    async with served(await a_driven_deployment()) as (octomate, app):
        async with over(octomate, app, DRIVEN_BEARER, mode=mode) as client:
            tools = await client.list_tools()

    assert [tool.name for tool in tools] == [
        *OCTOMATE_TOOLS,
        LIST_MCPS,
        LIST_MCP_TOOLS,
        CALL_MCP_TOOL,
        CONNECT_TOOL,
        CONFIRM_TOOL,
    ]


@pytest.mark.parametrize("mode", ["legacy", "2026-07-28"])
async def test_a_served_call_runs_against_the_turn_its_header_names(
    mode: Literal["legacy", "2026-07-28"],
) -> None:
    async with served(await a_driven_deployment()) as (octomate, app):
        session = await a_driven_turn(octomate)
        async with over(
            octomate,
            app,
            {**DRIVEN_BEARER, CONVERSATION_HEADER: str(session.conversation_id)},
            mode=mode,
        ) as client:
            result = await client.call_tool("gateway_scry", {"reveal": "destinations"})

    assert result.data == "\n".join(
        str(one) for one in await session.scry("destinations")
    )


async def test_a_driven_turn_answers_only_its_kickers_bearer() -> None:
    # `hui` is registered too, but the turn was kicked by `lu`: a valid API
    # token opens the endpoint, and the gateway still refuses to let it drive
    # someone else's session.
    user = await a_user("lu", profiles={"im": "alice"})
    await a_api_key(user, "lu-token")
    user = await a_user("hui")
    await a_api_key(user, "hui-token")
    octomate = Octomate(config=OctomateConfig(auth=auth_config()))
    async with served(octomate) as (octomate, app):
        session = await a_driven_turn(octomate)
        header = {CONVERSATION_HEADER: str(session.conversation_id)}
        async with over(
            octomate, app, {"Authorization": "Bearer hui-token", **header}
        ) as client:
            with pytest.raises(ToolError, match="not this bearer's to drive"):
                await client.call_tool("gateway_scry", {"reveal": "routes"})


async def test_a_call_naming_no_turn_is_refused() -> None:
    async with served(await a_driven_deployment()) as (octomate, app):
        await a_driven_turn(octomate)

        async with over(octomate, app, DRIVEN_BEARER) as client:
            with pytest.raises(ToolError, match="names no identity"):
                await client.call_tool("gateway_scry", {"reveal": "routes"})

        stray = {**DRIVEN_BEARER, CONVERSATION_HEADER: "not-a-uuid"}
        async with over(octomate, app, stray) as client:
            with pytest.raises(ToolError, match="not a conversation id"):
                await client.call_tool("gateway_scry", {"reveal": "routes"})

        unknown = {**DRIVEN_BEARER, CONVERSATION_HEADER: str(uuid.uuid4())}
        async with over(octomate, app, unknown) as client:
            with pytest.raises(ToolError, match="No turn of conversation"):
                await client.call_tool("gateway_scry", {"reveal": "routes"})


async def a_native_deployment() -> FakeOctomate:
    """A registered native Claude session with a linked account on `im`."""
    user = await a_user("luhui", profiles={"im": "alice"})
    await a_api_key(user, "luhui-token")
    octomate = FakeOctomate(
        config=OctomateConfig.model_validate(
            {"agents": {"claude": {}}, "auth": auth_config()}
        ),
    )
    octomate.connect(FakeAgent(id="other"))
    octomate.connect(
        FakeChannelTentacle(
            octomate=octomate,
            config=ChannelConfig(
                type="fake",
                agents=["other"],
            ),
        )
    )
    return octomate


NATIVE = {CLIENT_HEADER: CLAUDE_NATIVE_ID}
USER_BEARER = {"Authorization": "Bearer luhui-token"}


async def test_a_client_header_naming_no_native_runtime_is_refused() -> None:
    async with served(await a_native_deployment()) as (octomate, app):
        async with over(
            octomate, app, {**USER_BEARER, CLIENT_HEADER: "emacs-native"}
        ) as client:
            with pytest.raises(ToolError, match="names no native runtime"):
                await client.call_tool("gateway_scry", {"reveal": "routes"})


async def test_a_native_call_runs_against_an_ephemeral_session() -> None:
    async with served(await a_native_deployment()) as (octomate, app):
        async with over(octomate, app, {**USER_BEARER, **NATIVE}) as client:
            result = await client.call_tool("gateway_scry", {"reveal": "destinations"})

    # The bearer named luhui, so their linked account's crossing is on offer, and
    # nothing was ever registered: the session lived exactly one call.
    assert "their direct messages on" in result.data
    assert octomate.gateway.sessions == {}


async def test_a_native_summon_kicks_exactly_one_handoff() -> None:
    octomate = await a_native_deployment()
    async with served(octomate) as (octomate, app):
        async with over(octomate, app, {**USER_BEARER, **NATIVE}) as client:
            result = await client.call_tool(
                "gateway_summon",
                {
                    "agent_id": "other",
                    "model": "test",
                    "destination": {"kind": "channel", "channel": "im"},
                    "hint": "Working on it",
                    "reason": "the operator asked",
                    "summon": "Please take this up.",
                },
            )
        await asyncio.gather(*octomate.background)

    assert result.data == "Summoning other (test) → im."
    assert isinstance(octomate, FakeOctomate)
    [signal] = octomate.kicks
    assert isinstance(signal, GatewayHandoffSignal)
    assert signal.agent_id == CLAUDE_NATIVE_ID
    # The handoff carries who the bearer named, so the summoned run knows whose
    # behalf it was asked on.
    assert signal.user_profile is not None
    assert signal.user_profile.name == "luhui"
    assert isinstance(signal.decision, SummonDecision)
    assert signal.decision.summon == "Please take this up."
