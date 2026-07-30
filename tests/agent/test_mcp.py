from __future__ import annotations

import uuid
from base64 import urlsafe_b64encode
from datetime import datetime, timedelta, timezone
from typing import Any

from fastmcp.client.transports import StreamableHttpTransport
from pydantic import AnyHttpUrl, SecretStr
from pydantic_ai.mcp import MCPToolset
from pydantic_ai.models.test import TestModel
from pydantic_ai.tools import DeferredToolRequests
from pydantic_ai.toolsets import (
    AbstractToolset,
    DeferredLoadingToolset,
    FunctionToolset,
    PrefixedToolset,
)
from sqlalchemy.ext.asyncio import AsyncEngine

from octomate import Octomate
from octomate.capabilities.github import GitHubCapability
from octomate.oauth.base import McpConnectionAuth
from octomate.capabilities.harness.agent import Agent
from octomate.capabilities.harness.mcp_cache import McpToolsetCache
from octomate.config import GitHubMcpConfig, McpServerConfig
from octomate.config.integrations import GITHUB_CONNECTOR_ID
from octomate.config.users import UserConfig
from octomate.database import async_session
from octomate.managers.oauth import OAuthConnector
from octomate.managers.user import UserManager
from octomate.oauth.github import GitHubDeviceOAuthFlow
from octomate.schemas.oauth import (
    DeviceAuthorizationResponse,
    DeviceOAuthFlow,
    OAuthFlowContext,
    OAuthGrant,
    OAuthPending,
)
from octomate.schemas.segments import MessageSegment
from octomate.schemas.user import UserProfile
from octomate.tentacles.agent.inkling import InklingTentacle, build_mcp_toolsets
from octomate.tentacles.agent.inkling.base import InklingOutput

ENCRYPTION_KEY = SecretStr(urlsafe_b64encode(bytes(range(32))).decode())


class StaticGitHubFlow(DeviceOAuthFlow):
    async def start(self, context: OAuthFlowContext) -> DeviceAuthorizationResponse:
        return DeviceAuthorizationResponse(
            verification_uri=AnyHttpUrl("https://github.com/login/device"),
            device_code=SecretStr("device-secret"),
            user_code=SecretStr("ABCD-EFGH"),
            expires_at=datetime.now(timezone.utc) + timedelta(minutes=15),
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


async def github_host_and_profile() -> tuple[
    Octomate,
    UserProfile,
    GitHubCapability,
]:
    users = UserManager(
        {
            "alice": UserConfig.model_validate(
                {
                    "profiles": {
                        "slack": {"channel_user_id": "U1"},
                        "lark": {"channel_user_id": "L1"},
                    }
                }
            )
        }
    )
    host = Octomate(users=users, oauth_encryption_key=ENCRYPTION_KEY)
    github = GitHubCapability(
        manager=host.oauth,
        connector=host.oauth.register(
            OAuthConnector(id=GITHUB_CONNECTOR_ID, flow=StaticGitHubFlow())
        ),
    )
    await users.reconcile()
    async with async_session() as session:
        profile = await session.one_or_none(
            UserProfile,
            expressions=[UserProfile["channel_user_id"] == "U1"],
        )
    assert profile is not None
    return host, profile, github


def _prefixed(toolset: AbstractToolset[None]) -> PrefixedToolset[None]:
    # Each server is wrapped DeferredLoadingToolset -> PrefixedToolset -> MCPToolset.
    assert isinstance(toolset, DeferredLoadingToolset)
    prefixed = toolset.wrapped
    assert isinstance(prefixed, PrefixedToolset)
    return prefixed


def _toolset(toolset: AbstractToolset[None]) -> MCPToolset[None]:
    inner = _prefixed(toolset).wrapped
    assert isinstance(inner, MCPToolset)
    return inner


def _transport(toolset: AbstractToolset[None]) -> StreamableHttpTransport:
    transport = _toolset(toolset).client.transport
    assert isinstance(transport, StreamableHttpTransport)
    return transport


def _auth(toolset: AbstractToolset[None]) -> McpConnectionAuth:
    # The credential rides the transport's auth rather than a fixed header, so the
    # same object that sends it sees the 401 that retires it.
    auth = _transport(toolset).auth
    assert isinstance(auth, McpConnectionAuth)
    return auth


LINEAR_URL = "https://mcp.linear.app/mcp"


def test_no_servers_returns_empty() -> None:
    assert build_mcp_toolsets({}) == []


def test_disabled_server_is_skipped() -> None:
    server = McpServerConfig(url=LINEAR_URL, token=SecretStr("lin_x"), enabled=False)
    assert build_mcp_toolsets({"linear": server}) == []


def test_github_toolset_is_deferred_with_bearer_header() -> None:
    manager = Octomate().oauth
    connector = manager.register(
        OAuthConnector(
            id=GITHUB_CONNECTOR_ID,
            flow=GitHubDeviceOAuthFlow(client_id="Iv1.test", scopes=[]),
        )
    )
    capability = GitHubCapability(
        manager=manager,
        connector=connector,
        profile=UserProfile(channel_tentacle_id="slack", channel_user_id="U1"),
        access_token=SecretStr("github-oauth-token"),
    )
    assert capability.toolset is not None

    assert _toolset(capability.toolset).id == "github"
    assert _prefixed(capability.toolset).prefix == "github"
    transport = _transport(capability.toolset)
    assert transport.url == "https://api.githubcopilot.com/mcp/"
    auth = _auth(capability.toolset)
    assert auth.access_token.get_secret_value() == "github-oauth-token"


def test_github_read_only_selects_readonly_endpoint() -> None:
    manager = Octomate().oauth
    connector = manager.register(
        OAuthConnector(
            id=GITHUB_CONNECTOR_ID,
            flow=GitHubDeviceOAuthFlow(client_id="Iv1.test", scopes=[]),
        )
    )
    capability = GitHubCapability(
        manager=manager,
        connector=connector,
        mcp_config=GitHubMcpConfig(read_only=True),
        profile=UserProfile(channel_tentacle_id="slack", channel_user_id="U1"),
        access_token=SecretStr("github-oauth-token"),
    )
    assert capability.toolset is not None

    assert (
        _transport(capability.toolset).url
        == "https://api.githubcopilot.com/mcp/readonly"
    )


def test_server_takes_its_id_and_prefix_from_its_key() -> None:
    server = McpServerConfig(url=LINEAR_URL, token=SecretStr("lin_x"))

    (toolset,) = build_mcp_toolsets({"linear": server})

    assert _toolset(toolset).id == "linear"
    assert _prefixed(toolset).prefix == "linear"
    assert _transport(toolset).url == LINEAR_URL
    assert _transport(toolset).headers == {"Authorization": "Bearer lin_x"}


def test_explicit_prefix_overrides_the_key() -> None:
    server = McpServerConfig(prefix="lin", url=LINEAR_URL, token=SecretStr("lin_x"))

    (toolset,) = build_mcp_toolsets({"linear": server})

    assert _toolset(toolset).id == "linear"
    assert _prefixed(toolset).prefix == "lin"


def test_every_configured_server_is_mounted() -> None:
    # The builder knows no vendors: it mounts whatever `mcp` holds.
    servers = {
        "linear": McpServerConfig(url=LINEAR_URL, token=SecretStr("lin_x")),
        "notion": McpServerConfig(
            url="https://mcp.notion.com/mcp", token=SecretStr("n_x")
        ),
    }

    toolsets = build_mcp_toolsets(servers)

    assert [_toolset(ts).id for ts in toolsets] == ["linear", "notion"]


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

    async def __aexit__(self, *args: Any) -> bool | None:
        self.exited += 1
        return None


class FailingToolset(FunctionToolset[None]):
    """Stands in for a remote MCP server whose session fails to `initialize`,
    raising when the agent tries to open it during warm-up."""

    async def __aenter__(self) -> FailingToolset:
        raise RuntimeError("Failed to initialize server session")

    async def __aexit__(self, *args: Any) -> bool | None:
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


def github_tentacle(
    host: Octomate,
    github: GitHubCapability,
) -> InklingTentacle:
    agent: Agent[None, InklingOutput] = Agent(
        TestModel(),
        deps_type=type(None),
        name="octomate-inkling",
        output_type=[str, list[MessageSegment], DeferredToolRequests],
    )
    return InklingTentacle("inkling", host, agent=agent, capabilities=[github])


async def test_entering_tentacle_enters_agent_toolsets_once() -> None:
    spy = SpyToolset()
    tentacle = _tentacle_with(spy)

    async with tentacle:
        assert (spy.entered, spy.exited) == (1, 0)

    assert (spy.entered, spy.exited) == (1, 1)


async def test_warm_up_failure_does_not_abort_startup() -> None:
    tentacle = _tentacle_with(FailingToolset())

    # A transient MCP `initialize` failure while warming must not propagate out of
    # tentacle startup; the agent is left unentered so runs reconnect on demand.
    async with tentacle:
        pass


async def test_unconnected_slack_user_receives_github_oauth_tools(
    in_memory_engine: AsyncEngine,
) -> None:
    host, profile, github = await github_host_and_profile()

    capabilities = await github_tentacle(host, github).user_capabilities(profile)

    assert len(capabilities) == 1
    assert isinstance(capabilities[0], GitHubCapability)
    assert capabilities[0].access_token is None


async def test_github_capability_instances_reuse_connector_and_mcp_session(
    in_memory_engine: AsyncEngine,
) -> None:
    host, profile, github = await github_host_and_profile()
    spy = SpyToolset()
    github.mcp_toolset_factory = lambda profile, access_token: spy
    await host.oauth.start(profile, "github")
    await host.oauth.complete_latest(profile, "github")
    tentacle = github_tentacle(host, github)

    # Two runs, two copies of the mounted capability — sharing its connector and the
    # one warm MCP session it holds.
    async with tentacle:
        [first] = await tentacle.user_capabilities(profile)
        [second] = await tentacle.user_capabilities(profile)

        assert isinstance(first, GitHubCapability)
        assert isinstance(second, GitHubCapability)
        assert first is not github and second is not github
        assert first.connector is github.connector
        assert second.connector is github.connector
        assert host.oauth.connector("github") is github.connector
        assert first.toolset is spy
        assert second.toolset is spy
        assert (spy.entered, spy.exited) == (1, 0)

        async with spy:
            pass
        assert (spy.entered, spy.exited) == (2, 1)

    assert (spy.entered, spy.exited) == (2, 2)


async def test_connected_slack_user_receives_their_github_mcp_token(
    in_memory_engine: AsyncEngine,
) -> None:
    host, profile, github = await github_host_and_profile()
    await host.oauth.start(profile, "github")
    await host.oauth.complete_latest(profile, "github")

    capabilities = await github_tentacle(host, github).user_capabilities(profile)

    assert len(capabilities) == 1
    capability = capabilities[0]
    assert isinstance(capability, GitHubCapability)
    assert capability.toolset is not None
    assert (
        _auth(capability.toolset).access_token.get_secret_value() == "github-user-token"
    )


async def test_visitor_receives_no_github_connection_tools(
    in_memory_engine: AsyncEngine,
) -> None:
    host, _profile, github = await github_host_and_profile()
    visitor = await host.users.ensure_profile(
        "slack",
        UserProfile(channel_user_id="visitor", name="Visitor"),
    )

    capabilities = await github_tentacle(host, github).user_capabilities(visitor)

    assert capabilities == []


async def test_registered_user_connects_from_any_channel(
    in_memory_engine: AsyncEngine,
) -> None:
    # The `users:` registry is the authority on who may connect, not the channel
    # they happen to be speaking from — every channel can present an authorization.
    host, _profile, github = await github_host_and_profile()
    async with async_session() as session:
        lark_profile = await session.one_or_none(
            UserProfile,
            expressions=[UserProfile["channel_user_id"] == "L1"],
        )
    assert lark_profile is not None

    [capability] = await github_tentacle(host, github).user_capabilities(lark_profile)

    assert isinstance(capability, GitHubCapability)
    assert capability.access_token is None


async def test_cache_reuses_and_warms_a_key_once() -> None:
    cache = McpToolsetCache()
    spy = SpyToolset()
    key = uuid.uuid4()

    async with cache:
        first = await cache.acquire(
            kind="github",
            key=key,
            fingerprint="t1",
            max_entries=32,
            warm_timeout=16.0,
            build=lambda: spy,
        )
        # A cache hit does not call build again; the same warm session is returned.
        second = await cache.acquire(
            kind="github",
            key=key,
            fingerprint="t1",
            max_entries=32,
            warm_timeout=16.0,
            build=lambda: SpyToolset(),
        )
        assert first is spy and second is spy
        assert (spy.entered, spy.exited) == (1, 0)

    assert (spy.entered, spy.exited) == (1, 1)


async def test_cache_rebuilds_and_closes_on_fingerprint_change() -> None:
    cache = McpToolsetCache()
    old, new = SpyToolset(), SpyToolset()
    key = uuid.uuid4()

    async with cache:
        first = await cache.acquire(
            kind="github",
            key=key,
            fingerprint="old",
            max_entries=32,
            warm_timeout=16.0,
            build=lambda: old,
        )
        second = await cache.acquire(
            kind="github",
            key=key,
            fingerprint="new",
            max_entries=32,
            warm_timeout=16.0,
            build=lambda: new,
        )
        assert first is old and second is new
        # A changed credential closes the stale session before serving the new one.
        assert (old.entered, old.exited) == (1, 1)
        assert (new.entered, new.exited) == (1, 0)

    assert (new.entered, new.exited) == (1, 1)


async def test_cache_evicts_least_recently_used_per_kind() -> None:
    cache = McpToolsetCache()
    keys = [uuid.uuid4() for _ in range(3)]
    spies = [SpyToolset() for _ in range(3)]

    async with cache:
        for key, spy in zip(keys, spies):
            await cache.acquire(
                kind="github",
                key=key,
                fingerprint="t",
                max_entries=2,
                warm_timeout=16.0,
                build=lambda spy=spy: spy,
            )
        # Admitting the third session past the bound evicts and closes the first.
        assert (spies[0].entered, spies[0].exited) == (1, 1)
        assert (spies[1].entered, spies[1].exited) == (1, 0)
        assert (spies[2].entered, spies[2].exited) == (1, 0)


async def test_cache_touch_refreshes_lru_recency() -> None:
    cache = McpToolsetCache()
    keys = [uuid.uuid4() for _ in range(3)]
    spies = [SpyToolset() for _ in range(3)]

    async def acquire(index: int) -> None:
        await cache.acquire(
            kind="github",
            key=keys[index],
            fingerprint="t",
            max_entries=2,
            warm_timeout=16.0,
            build=lambda: spies[index],
        )

    async with cache:
        await acquire(0)
        await acquire(1)
        # Re-acquiring keys[0] makes keys[1] the least-recently-used entry.
        await acquire(0)
        await acquire(2)
        assert (spies[1].entered, spies[1].exited) == (1, 1)
        assert (spies[0].entered, spies[0].exited) == (1, 0)
        assert (spies[2].entered, spies[2].exited) == (1, 0)


async def test_cache_bounds_each_kind_independently() -> None:
    cache = McpToolsetCache()
    github_spy, linear_spy = SpyToolset(), SpyToolset()

    async with cache:
        await cache.acquire(
            kind="github",
            key=uuid.uuid4(),
            fingerprint="t",
            max_entries=1,
            warm_timeout=16.0,
            build=lambda: github_spy,
        )
        await cache.acquire(
            kind="linear",
            key=uuid.uuid4(),
            fingerprint="t",
            max_entries=1,
            warm_timeout=16.0,
            build=lambda: linear_spy,
        )
        # A one-slot bound on github does not evict a different kind's session.
        assert (github_spy.entered, github_spy.exited) == (1, 0)
        assert (linear_spy.entered, linear_spy.exited) == (1, 0)
