"""The MCP tentacles: a vendor's server spoken to with one operator
credential, a person's Linear or GitHub — all under `mcp:` — composed from config,
proxied on the served server as the caller, and mounted in process by Inkling for
the person of its turn.
"""

from __future__ import annotations

from base64 import urlsafe_b64encode
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from types import TracebackType

import httpx
import pytest
from fastmcp import Client, FastMCP
from fastmcp.exceptions import ToolError
from fastmcp.server.dependencies import get_http_headers
from pydantic import AnyHttpUrl, SecretStr
from pydantic_ai.models.test import TestModel
from pydantic_ai.tools import DeferredToolRequests
from pydantic_ai.toolsets import AbstractToolset, FunctionToolset
from sqlalchemy.ext.asyncio import AsyncEngine

from octomate import Octomate
from octomate.capabilities.harness.agent import Agent
from octomate.config import BareMcpConfig, GitHubMcpConfig, LinearMcpConfig
from octomate.config.users import UserConfig
from octomate.database import async_session
from octomate.managers.gateway import GatewaySession
from octomate.managers.oauth import OAuthConnector
from octomate.managers.user import UserManager
from octomate.mcp.oauth import CONFIRM_TOOL, CONNECT_TOOL
from octomate.mcp.server import tentacles_mcp
from octomate.schemas.oauth import (
    DeviceAuthorizationResponse,
    DeviceOAuthFlow,
    DirectHttpOAuthCallbackTransport,
    OAuthFlowContext,
    OAuthGrant,
    OAuthPending,
)
from octomate.schemas.segments import MessageSegment
from octomate.schemas.user import UserProfile
from octomate.tentacles.github import GitHubTentacle
from octomate.tentacles.inkling import InklingTentacle
from octomate.tentacles.inkling.base import InklingOutput
from octomate.tentacles.linear import LinearTentacle
from octomate.tentacles.mcp import BareMcpTentacle, OAuthMcpTentacle, build_mcp
from tests.channels.slack.test_mcp import into
from tests.support.managers import fixed_session

ENCRYPTION_KEY = SecretStr(urlsafe_b64encode(bytes(range(32))).decode())
LINEAR_URL = "https://mcp.linear.app/mcp"


@pytest.fixture(autouse=True)
async def db(in_memory_engine: AsyncEngine) -> None:
    return


class StaticGitHubFlow(DeviceOAuthFlow):
    async def start(self, context: OAuthFlowContext) -> DeviceAuthorizationResponse:
        return DeviceAuthorizationResponse(
            verification_uri=AnyHttpUrl("https://github.com/login/device"),
            device_code=SecretStr("device-secret"),
            user_code=SecretStr("ABCD-EFGH"),
            expires_at=datetime.now(UTC) + timedelta(minutes=15),
            interval_seconds=5,
        )

    async def complete(
        self,
        context: OAuthFlowContext,
        device_code: SecretStr,
    ) -> OAuthGrant | OAuthPending:
        return OAuthGrant(
            access_token=SecretStr("github-user-token"),
            subject="42",
            account_label="alice-gh",
        )


class Provider(OAuthMcpTentacle):
    """A person's account with some provider, linked under `gh`, over a server
    that stands in for the vendor's."""

    label = "Provider"
    upstream = "https://mcp.example/mcp"
    instructions = "## Provider\n\nThe provider's own contract.\n"
    prefix = "gh"


async def a_linked_host() -> tuple[Octomate, Provider, UserProfile]:
    """A host whose registered `alice` may link `gh`, with her Slack profile."""
    users = UserManager(
        {
            "alice": UserConfig.model_validate(
                {"profiles": {"slack": {"channel_user_id": "U1"}}}
            )
        }
    )
    host = Octomate(users=users, oauth_encryption_key=ENCRYPTION_KEY)
    tentacle = host.connect(Provider("gh", host))
    host.oauth.register(OAuthConnector(id="gh", flow=StaticGitHubFlow()))
    await users.reconcile()
    async with async_session() as session:
        profile = await session.one_or_none(
            UserProfile,
            expressions=[UserProfile["channel_user_id"] == "U1"],
        )
    assert profile is not None
    return host, tentacle, profile


def an_upstream(tool: str) -> tuple[FastMCP, list[str]]:
    """A provider's server as far as one tool goes, recording the bearer each
    call arrived with."""
    seen: list[str] = []
    upstream = FastMCP("upstream")

    @upstream.tool(name=tool)
    async def answer() -> str:
        """What the provider says of its tool."""
        seen.append(get_http_headers(include_all=True).get("authorization", ""))
        return "answered"

    return upstream, seen


def a_turn(profile: UserProfile | None = None) -> GatewaySession:
    """A turn by `profile`, or by nobody registered — the session a proxied call
    resolves to."""
    return GatewaySession(
        channel_routes={}, current_agent_id="inkling", user_profile=profile
    )


@asynccontextmanager
async def upstream_of(
    upstream: FastMCP,
) -> AsyncIterator[httpx.AsyncBaseTransport]:
    """`upstream` served, as a transport a proxy's client can be routed into."""
    app = upstream.http_app()
    async with app.router.lifespan_context(app):
        yield httpx.ASGITransport(app=app)


@asynccontextmanager
async def proxied(
    tentacle: BareMcpTentacle | OAuthMcpTentacle,
    session: GatewaySession,
    upstream: FastMCP,
) -> AsyncIterator[Client]:
    """The tentacles' server for `session`, `tentacle`'s upstream being `upstream`."""
    async with upstream_of(upstream) as transport:
        server = tentacles_mcp(
            fixed_session(session), [tentacle], httpx_client_factory=into(transport)
        )
        async with Client(server) as client:
            yield client


async def test_a_bare_server_speaks_the_operator_credential_for_everyone() -> None:
    tentacle = BareMcpTentacle(
        "linear",
        Octomate(),
        config=BareMcpConfig(url=LINEAR_URL, token=SecretStr("lin_x")),
    )
    upstream, seen = an_upstream("list_issues")

    # A turn by nobody registered still speaks: the credential is the
    # deployment's, and the key is the prefix its tools carry.
    async with proxied(tentacle, a_turn(), upstream) as client:
        tools = await client.list_tools()
        result = await client.call_tool("linear_list_issues", {})

    assert [tool.name for tool in tools] == ["linear_list_issues"]
    assert result.data == "answered"
    assert seen == ["Bearer lin_x"]


async def test_an_explicit_prefix_overrides_the_key() -> None:
    tentacle = BareMcpTentacle(
        "linear",
        Octomate(),
        config=BareMcpConfig(prefix="lin", url=LINEAR_URL, token=SecretStr("x")),
    )
    upstream, _seen = an_upstream("list_issues")

    async with proxied(tentacle, a_turn(), upstream) as client:
        tools = await client.list_tools()

    assert [tool.name for tool in tools] == ["lin_list_issues"]


def test_bootstrap_composes_each_mcp_type_and_keys_it_by_name() -> None:
    # `type` is the only place a provider is named; the configured key is the
    # tentacle id, the connector id and the prefix throughout.
    host = Octomate()
    notion = build_mcp(
        "notion",
        BareMcpConfig(url="https://mcp.notion.com/mcp", token=SecretStr("ntn_x")),
        host,
    )
    github = build_mcp("gh", GitHubMcpConfig(client_id="Iv1.test"), host)
    linear = build_mcp("linear_home", LinearMcpConfig(client_id="lin"), host)

    assert isinstance(notion, BareMcpTentacle)
    assert isinstance(github, GitHubTentacle)
    assert isinstance(linear, LinearTentacle)
    assert (notion.id, notion.prefix, notion.upstream) == (
        "notion",
        "notion",
        "https://mcp.notion.com/mcp",
    )
    assert (github.id, github.prefix, github.upstream) == (
        "gh",
        "gh",
        "https://api.githubcopilot.com/mcp/",
    )
    assert (linear.id, linear.prefix, linear.upstream) == (
        "linear_home",
        "linear_home",
        LINEAR_URL,
    )
    assert sorted(host.oauth.connectors) == ["gh", "linear_home"]
    # Only the authorization-code half carries a transport, and it is what makes
    # `Octomate.app` serve the routes its URIs point at.
    assert host.oauth.connector("gh").callback_transport is None
    assert isinstance(
        host.oauth.connector("linear_home").callback_transport,
        DirectHttpOAuthCallbackTransport,
    )


def test_two_accounts_of_one_vendor_get_their_own_connectors_and_prefixes() -> None:
    host = Octomate()
    work = build_mcp("linear_work", LinearMcpConfig(client_id="a"), host)
    home = build_mcp("linear_home", LinearMcpConfig(client_id="b"), host)

    # Separate connectors, so separate stored connections and separate tool
    # names — the model is never offered two identically named sets.
    assert sorted(host.oauth.connectors) == ["linear_home", "linear_work"]
    assert (work.prefix, home.prefix) == ("linear_work", "linear_home")


def test_a_configured_prefix_overrides_the_id() -> None:
    # The id is durable — it keys stored connections — so the prefix is what
    # moves when one vendor is mounted twice.
    linear = build_mcp(
        "linear_personal", LinearMcpConfig(client_id="a", prefix="linme"), Octomate()
    )

    assert (linear.id, linear.prefix) == ("linear_personal", "linme")


def test_read_only_selects_the_readonly_endpoint() -> None:
    github = build_mcp(
        "gh", GitHubMcpConfig(client_id="Iv1.test", read_only=True), Octomate()
    )

    assert github.upstream == "https://api.githubcopilot.com/mcp/readonly"


async def test_a_linked_person_speaks_with_their_own_token() -> None:
    host, tentacle, profile = await a_linked_host()
    upstream, seen = an_upstream("list_repos")

    async with proxied(tentacle, a_turn(profile), upstream) as client:
        unlinked = await client.list_tools()
        with pytest.raises(ToolError, match=f"`{CONNECT_TOOL}` with `gh`"):
            await client.call_tool("gh_list_repos", {})
        await host.oauth.start(profile, "gh")
        await host.oauth.complete_latest(profile, "gh")
        linked = await client.list_tools()
        result = await client.call_tool("gh_list_repos", {})

    # Before the link: the linking pair and nothing of the provider's. After:
    # the provider's tools under the prefix, called as the person.
    assert [tool.name for tool in unlinked] == [CONNECT_TOOL, CONFIRM_TOOL]
    assert [tool.name for tool in linked] == [
        CONNECT_TOOL,
        CONFIRM_TOOL,
        "gh_list_repos",
    ]
    assert result.data == "answered"
    assert seen == ["Bearer github-user-token"]


async def test_a_turn_by_nobody_registered_gets_nothing_of_a_persons_provider() -> None:
    _host, tentacle, _profile = await a_linked_host()
    upstream, seen = an_upstream("list_repos")

    async with proxied(tentacle, a_turn(), upstream) as client:
        tools = await client.list_tools()
        with pytest.raises(ToolError, match="nobody registered did"):
            await client.call_tool("gh_list_repos", {})

    assert [tool.name for tool in tools] == [CONNECT_TOOL, CONFIRM_TOOL]
    assert seen == []


class SpyToolset(FunctionToolset[None]):
    """Counts how many times the agent enters/exits it, standing in for an MCP
    server's warm session without a real connection."""

    def __init__(self) -> None:
        super().__init__()
        self.entered = 0
        self.exited = 0

    async def __aenter__(self) -> SpyToolset:
        self.entered += 1
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> bool | None:
        self.exited += 1
        return None


class FailingToolset(FunctionToolset[None]):
    """Stands in for a remote MCP server whose session fails to `initialize`,
    raising when the agent tries to open it during warm-up."""

    async def __aenter__(self) -> FailingToolset:
        raise RuntimeError("Failed to initialize server session")

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> bool | None:
        return None


def _tentacle_with(toolset: AbstractToolset[None]) -> InklingTentacle:
    octomate = Octomate()
    agent: Agent[None, InklingOutput] = Agent(
        TestModel(),
        deps_type=type(None),
        name="octomate-inkling",
        output_type=[str, list[MessageSegment], DeferredToolRequests],
        toolsets=[toolset],
    )
    return InklingTentacle("inkling", octomate, agent=agent)


async def test_entering_tentacle_enters_agent_toolsets_once() -> None:
    spy = SpyToolset()
    tentacle = _tentacle_with(spy)

    async with tentacle:
        # Warming runs behind the enter; the warm state is what the task settles.
        assert tentacle.warm_task is not None
        await tentacle.warm_task
        assert (spy.entered, spy.exited) == (1, 0)

    assert (spy.entered, spy.exited) == (1, 1)


async def test_warm_up_failure_does_not_abort_startup() -> None:
    tentacle = _tentacle_with(FailingToolset())

    # A transient MCP `initialize` failure while warming must not propagate out of
    # tentacle startup or its background warm task; the agent is left unentered so
    # runs reconnect on demand.
    async with tentacle:
        assert tentacle.warm_task is not None
        await tentacle.warm_task
