"""Slack's own tools proxied on Octomate's one server, as the person who drove
the turn.

Spoken to over the wire where the identity comes from the request — the served
list, the refusals, and the connection round trip through the deployment's own
OAuth routes — and in memory where the upstream has to be stood in for: the one
call that reaches Slack carries that person's token, and a token Slack has since
revoked retires the connection.
"""

from __future__ import annotations

import uuid
from base64 import urlsafe_b64encode
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Self, cast
from urllib.parse import parse_qs

import httpx
import pytest
from fastapi import FastAPI
from fastmcp import Client, FastMCP
from fastmcp.exceptions import ToolError
from fastmcp.server.dependencies import get_http_headers
from mcp.shared._httpx_utils import McpHttpClientFactory
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import AsyncEngine

from octomate.base import Octomate
from octomate.config import AgentModelConfig, SlackChannelConfig, SlackStreamConfig
from octomate.config.base import OctomateConfig
from octomate.config.channels import SlackOAuthClientConfig
from octomate.config.users import UserConfig
from octomate.managers.gateway import GatewaySession
from octomate.managers.oauth import OAuthConnector
from octomate.managers.user import UserManager
from octomate.mcp.gateway import CONVERSATION_HEADER
from octomate.mcp.oauth import CONFIRM_TOOL, CONNECT_TOOL
from octomate.mcp.server import octomate_instructions, octomate_mcp
from octomate.schemas.conversation import ChannelAddress
from octomate.schemas.oauth import DirectHttpOAuthCallbackTransport
from octomate.tentacles.slack import SlackChromo, SlackTentacle
from octomate.tentacles.slack.ink import SlackInk
from octomate.tentacles.slack.oauth import SlackAuthorizationCodeOAuthFlow
from tests.agent.test_mcp_serving import OCTOMATE_TOOLS, over, served
from tests.channels.slack.fakes import FakeSlackInk, compose_slack_feelers
from tests.channels.slack.test_oauth import slack_transport
from tests.support.channels import FakeChannelTentacle
from tests.support.managers import FakeThreadManager, fixed_session

ENCRYPTION_KEY = SecretStr(urlsafe_b64encode(bytes(range(32))).decode())
BEARER = {"Authorization": "Bearer steve-token"}
SLACK = {"provider": "slack"}
# What every caller is listed on a deployment with a Slack workspace: Octomate's
# own families and the linking pair; Slack's tools only once they have linked.
LISTED_TO_ALL = [*OCTOMATE_TOOLS, CONNECT_TOOL, CONFIRM_TOOL]


@pytest.fixture(autouse=True)
async def db(in_memory_engine: AsyncEngine) -> None:
    return


class ServedSlackTentacle(SlackTentacle):
    """The Slack tentacle as the served app starts it, minus the socket: the app's
    lifespan enters every channel, and a test workspace has no Slack to reach."""

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None


def a_deployment() -> Octomate:
    """A deployment whose driven turns are kicked by the registered `steve`, who
    is `U1` on the `slack` workspace."""
    return Octomate(
        config=OctomateConfig.model_validate(
            {"users": {"steve": {"secret": "steve-token"}}}
        ),
        users=UserManager(
            {
                "steve": UserConfig.model_validate(
                    {
                        "secret": "steve-token",
                        "profiles": {"slack": {"channel_user_id": "U1"}},
                    }
                )
            }
        ),
        oauth_encryption_key=ENCRYPTION_KEY,
    )


def a_workspace(
    octomate: Octomate, ink: FakeSlackInk, id: str = "slack"
) -> SlackTentacle:
    """A Slack workspace offering its tools, connected the way bootstrap connects
    one: its connector registered on the deployment's OAuth manager, its flow
    speaking to a stand-in for Slack that grants `xoxp-user` to `steve.li`."""
    channel = object.__new__(ServedSlackTentacle)
    channel.id = id
    channel.ink = cast(SlackInk, ink)
    channel.chromo = SlackChromo()
    config = SlackChannelConfig(
        app_id="A-test",
        bot_token=SecretStr("xoxb-test"),
        app_token=SecretStr("xapp-test"),
        stream=SlackStreamConfig(flush_interval=0),
        agents=[AgentModelConfig(agent="codex", model="test")],
        mcp=True,
        oauth=SlackOAuthClientConfig(client_id="1.2", client_secret=SecretStr("shh")),
    )
    channel.config = config
    channel.app_token = config.app_token
    compose_slack_feelers(channel)
    octomate.connect(channel)
    assert config.oauth is not None
    octomate.oauth.register(
        OAuthConnector(
            id=id,
            flow=SlackAuthorizationCodeOAuthFlow(
                client_id=config.oauth.client_id,
                client_secret=config.oauth.client_secret,
                scopes=config.oauth.scopes,
                transport=slack_transport(
                    {"ok": True, "access_token": "xoxp-user", "token_type": "user"}
                ),
            ),
            callback_transport=DirectHttpOAuthCallbackTransport(
                config.oauth.callback_base_uri
            ),
        )
    )
    return channel


async def a_slack_turn(
    octomate: Octomate, channel: SlackTentacle, address: ChannelAddress | None
) -> GatewaySession:
    """A turn at the gateway kicked by `steve`, on `address` — a Slack thread,
    another channel, or nowhere at all."""
    session = GatewaySession(
        channel_routes={channel.id: []},
        current_agent_id="codex",
        channels={channel.id: channel, "im": FakeChannelTentacle()},
        conversation_id=uuid.uuid4(),
        conversation_address=address,
        users=octomate.users,
        user_profile=await octomate.users.profile(channel.id, "U1"),
    )
    octomate.gateway.register(session)
    return session


def a_slack_thread() -> ChannelAddress:
    return ChannelAddress(
        channel_tentacle_id="slack",
        chat_type="thread",
        chat_id="C1",
        user_id="U1",
        channel_thread_id="1710000000.000100",
        shared=True,
    )


def naming(session: GatewaySession) -> dict[str, str]:
    return {**BEARER, CONVERSATION_HEADER: str(session.conversation_id)}


def button_url(ink: FakeSlackInk) -> str:
    """The one link the workspace was sent: the button on the card in `ink`."""
    [(_, _, [message], _, _)] = ink.sent
    assert message.blocks is not None
    elements = message.blocks[-1]["elements"]
    assert isinstance(elements, list)
    [button] = elements
    assert isinstance(button, dict)
    url = button["url"]
    assert isinstance(url, str)
    return url


def browser(app: FastAPI) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://localhost:8000"
    )


async def connected(app: FastAPI, ink: FakeSlackInk, client: Client) -> None:
    """Link `steve`'s account as they would: ask for the link, open it, and come
    back from Slack with a code."""
    result = await client.call_tool(CONNECT_TOOL, SLACK)
    assert result.data == (
        "The authorization link is on its way to this user's direct messages."
    )
    async with browser(app) as http:
        opened = await http.get(button_url(ink))
        assert opened.status_code == 307
        query = parse_qs(httpx.URL(opened.headers["location"]).query.decode())
        returned = await http.get(
            "/oauth/slack/callback",
            params={"state": query["state"][0], "code": "auth-code"},
        )
    assert returned.status_code == 200
    assert "Connected as steve.li in Ancher" in returned.text


def a_slack_upstream() -> tuple[FastMCP, list[str]]:
    """Slack's server as far as one tool goes, recording the bearer each call
    arrived with."""
    seen: list[str] = []
    upstream = FastMCP("slack-upstream")

    @upstream.tool(name="slack_read_user_profile")
    async def read_user_profile(user_id: str | None = None) -> str:
        seen.append(get_http_headers(include_all=True).get("authorization", ""))
        return "steve.li"

    return upstream, seen


def into(transport: httpx.AsyncBaseTransport) -> McpHttpClientFactory:
    """An httpx client factory routing the proxy's calls into `transport` instead
    of Slack, with everything the proxy set on the client — the auth above all."""

    def factory(
        headers: dict[str, str] | None = None,
        timeout: httpx.Timeout | None = None,
        auth: httpx.Auth | None = None,
        follow_redirects: bool = True,
    ) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            transport=transport,
            base_url="https://mcp.slack.com",
            headers=headers,
            timeout=timeout,
            auth=auth,
            follow_redirects=follow_redirects,
        )

    return factory


@asynccontextmanager
async def in_memory(
    channel: SlackTentacle, session: GatewaySession, transport: httpx.AsyncBaseTransport
) -> AsyncIterator[Client]:
    """The server mounted for one fixed turn on `channel`, Slack's upstream being
    `transport`."""
    server = octomate_mcp(
        fixed_session(session),
        FakeThreadManager(),
        tentacles=[channel],
        httpx_client_factory=into(transport),
    )
    async with Client(server) as client:
        yield client


def test_every_slack_workspace_is_a_provider_and_slack_is_proxied_once() -> None:
    octomate = a_deployment()
    a_workspace(octomate, FakeSlackInk())
    a_workspace(octomate, FakeSlackInk(), id="slack-b")

    tentacles = list(octomate.mcps.values())
    instructions = octomate_instructions(tentacles)

    assert list(octomate.mcps) == ["slack", "slack-b"]
    assert f"`{CONNECT_TOOL}` with the provider's id (`slack`, `slack-b`)" in (
        instructions
    )
    assert instructions.count("## Slack") == 1


async def test_a_caller_with_no_turn_is_listed_no_slack_tool() -> None:
    octomate = a_deployment()
    a_workspace(octomate, FakeSlackInk())
    async with served(octomate) as (octomate, app):
        async with over(octomate, app, BEARER) as client:
            tools = await client.list_tools()

    assert [tool.name for tool in tools] == LISTED_TO_ALL


async def test_a_call_outside_a_slack_turn_is_refused() -> None:
    octomate = a_deployment()
    channel = a_workspace(octomate, FakeSlackInk())
    async with served(octomate) as (octomate, app):
        elsewhere = await a_slack_turn(
            octomate,
            channel,
            ChannelAddress(
                channel_tentacle_id="im",
                chat_type="group",
                chat_id="room",
                user_id="U1",
                shared=True,
            ),
        )
        async with over(octomate, app, naming(elsewhere)) as client:
            with pytest.raises(ToolError, match="is on im, not Slack"):
                await client.call_tool("slack_read_user_profile", {})

        # Linking asks nothing of the turn's channel but a place to deliver the
        # card; Slack's own refusal is for Slack's tools.
        nowhere = await a_slack_turn(octomate, channel, None)
        async with over(octomate, app, naming(nowhere)) as client:
            with pytest.raises(ToolError, match="nowhere in Slack to act"):
                await client.call_tool("slack_read_user_profile", {})
            with pytest.raises(ToolError, match="no turn on a channel"):
                await client.call_tool(CONNECT_TOOL, SLACK)


async def test_an_unconnected_caller_is_listed_nothing_and_told_to_connect() -> None:
    octomate = a_deployment()
    channel = a_workspace(octomate, FakeSlackInk())
    async with served(octomate) as (octomate, app):
        session = await a_slack_turn(octomate, channel, a_slack_thread())
        async with over(octomate, app, naming(session)) as client:
            tools = await client.list_tools()
            with pytest.raises(ToolError, match=f"`{CONNECT_TOOL}` with `slack`"):
                await client.call_tool("slack_read_user_profile", {})
            status = await client.call_tool(CONFIRM_TOOL, SLACK)

    assert [tool.name for tool in tools] == LISTED_TO_ALL
    assert status.data == (
        "Slack is not connected yet. The link finishes the connection by itself "
        "once they approve it; there is nothing to do here but wait and check again."
    )


async def test_the_link_goes_to_their_direct_messages_and_nowhere_else() -> None:
    octomate = a_deployment()
    ink = FakeSlackInk()
    channel = a_workspace(octomate, ink)
    async with served(octomate) as (octomate, app):
        session = await a_slack_turn(octomate, channel, a_slack_thread())
        async with over(octomate, app, naming(session)) as client:
            result = await client.call_tool(CONNECT_TOOL, SLACK)

    # Asked from a shared thread, so the card went to `U1`'s own DM, and the
    # model was given nothing it could repeat into the thread.
    [(chat_id, chat_type, _, _, _)] = ink.sent
    assert (chat_id, chat_type) == ("D-U1", "dm")
    link = button_url(ink)
    assert link.startswith("http://localhost:8000/oauth/slack/start/")
    assert isinstance(result.data, str)
    assert link not in result.data


async def test_a_connected_caller_is_listed_slacks_tools_and_acts_as_themselves() -> (
    None
):
    octomate = a_deployment()
    ink = FakeSlackInk()
    channel = a_workspace(octomate, ink)
    upstream, seen = a_slack_upstream()
    upstream_app = upstream.http_app()
    async with served(octomate) as (octomate, app):
        session = await a_slack_turn(octomate, channel, a_slack_thread())
        async with over(octomate, app, naming(session)) as client:
            await connected(app, ink, client)
            status = await client.call_tool(CONFIRM_TOOL, SLACK)
        assert status.data == (
            "Slack is connected: its tools now act as this user here."
        )

        async with (
            upstream_app.router.lifespan_context(upstream_app),
            in_memory(
                channel, session, httpx.ASGITransport(app=upstream_app)
            ) as client,
        ):
            tools = await client.list_tools()
            result = await client.call_tool("slack_read_user_profile", {})

    # The list is Slack's, fetched for this person and listed after Octomate's
    # own tools, and the call reached Slack as them, with the token Octomate
    # holds — which the driven runtime never saw.
    assert [tool.name for tool in tools] == [
        *LISTED_TO_ALL,
        "slack_read_user_profile",
    ]
    assert result.data == "steve.li"
    assert seen == ["Bearer xoxp-user"]


async def test_a_token_slack_has_revoked_retires_the_connection() -> None:
    octomate = a_deployment()
    ink = FakeSlackInk()
    channel = a_workspace(octomate, ink)
    async with served(octomate) as (octomate, app):
        session = await a_slack_turn(octomate, channel, a_slack_thread())
        async with over(octomate, app, naming(session)) as client:
            await connected(app, ink, client)

        refusing = httpx.MockTransport(lambda request: httpx.Response(401))
        async with in_memory(channel, session, refusing) as client:
            with pytest.raises(ToolError):
                await client.call_tool("slack_read_user_profile", {})

        async with over(octomate, app, naming(session)) as client:
            status = await client.call_tool(CONFIRM_TOOL, SLACK)

    assert status.data == (
        "Slack was connected and is not any more — the authorization was revoked "
        f"or expired. Offer to send a fresh link with `{CONNECT_TOOL}`."
    )
