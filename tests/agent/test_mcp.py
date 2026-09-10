"""The MCP tentacles: a vendor's server spoken to with one operator
credential, a person's Linear or GitHub — all under `mcp:` — composed from config,
proxied on the served server as the caller, and mounted in process by Inkling for
the person of its turn.
"""

from __future__ import annotations

from base64 import urlsafe_b64encode
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from types import TracebackType

import httpx2
import pytest
from fastmcp import Client, FastMCP
from fastmcp.exceptions import ToolError
from fastmcp.server.dependencies import get_http_headers
from pydantic import AnyHttpUrl, SecretStr
from pydantic_ai import RunContext
from pydantic_ai.exceptions import ModelRetry
from pydantic_ai.messages import ModelMessage
from pydantic_ai.models import Model
from pydantic_ai.models.function import (
    AgentInfo,
    DeltaToolCall,
    DeltaToolCalls,
    FunctionModel,
)
from pydantic_ai.models.test import TestModel
from pydantic_ai.tools import DeferredToolRequests
from pydantic_ai.toolsets import AbstractToolset, FunctionToolset
from pydantic_ai.usage import RunUsage
from sqlalchemy.ext.asyncio import AsyncEngine
from uuid_utils.compat import uuid7

from octomate import Octomate
from octomate.capabilities.gateway import GatewayCapability
from octomate.capabilities.harness.agent import Agent
from octomate.capabilities.mcp import TentaclesToolset, tentacles_capability
from octomate.database import async_session
from octomate.managers.gateway import OctomateSession
from octomate.managers.oauth import OAuthConnector
from octomate.managers.user import UserManager
from octomate.mcp.oauth import CONFIRM_TOOL, CONNECT_TOOL
from octomate.mcp.server import (
    CALL_MCP_TOOL,
    DISABLE_MCP,
    ENABLE_MCP,
    INSTALL_MCP,
    LIST_MCP_TENTACLES,
    LIST_MCP_TOOLS,
    LIST_MCPS,
    UNINSTALL_MCP,
    tentacles_mcp,
)
from octomate.schemas.conversation import ChannelAddress
from octomate.schemas.oauth import (
    DeviceAuthorizationResponse,
    DeviceOAuthFlow,
    OAuthFlowContext,
    OAuthGrant,
    OAuthPending,
)
from octomate.schemas.segments import MessageSegment
from octomate.schemas.user import UserProfile
from octomate.tentacles.inkling import InklingTentacle
from octomate.tentacles.inkling.base import InklingOutput
from octomate.tentacles.mcp import (
    BareMcpTentacle,
    OAuthMcpTentacle,
)
from tests.channels.slack.test_mcp import into
from tests.support.managers import FakeConversationManager, fixed_session
from tests.support.mcp import discover, install_tentacle
from tests.support.users import a_user

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


async def a_linked_host() -> tuple[Octomate, Provider, UserProfile]:
    """A host whose registered `alice` may link `gh`, with her Slack profile."""
    await a_user("alice", profiles={"slack": "U1"})
    users = UserManager()
    host = Octomate(users=users, oauth_encryption_key=ENCRYPTION_KEY)
    tentacle = host.connect(Provider("gh", host))
    host.oauth.register(OAuthConnector(id="gh", flows=[StaticGitHubFlow()]))
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


def a_turn(profile: UserProfile | None = None) -> OctomateSession:
    """A turn by `profile`, or by nobody registered — the session a proxied call
    resolves to."""
    return OctomateSession(
        channel_routes={}, current_agent_id="inkling", user_profile=profile
    )


@asynccontextmanager
async def upstream_of(
    upstream: FastMCP,
) -> AsyncIterator[httpx2.AsyncBaseTransport]:
    """`upstream` served, as a transport a proxy's client can be routed into."""
    app = upstream.http_app()
    async with app.router.lifespan_context(app):
        yield httpx2.ASGITransport(app=app)


@asynccontextmanager
async def proxied(
    tentacle: BareMcpTentacle | OAuthMcpTentacle,
    session: OctomateSession,
    upstream: FastMCP,
) -> AsyncIterator[Client]:
    """The tentacles' server for `session`, `tentacle`'s upstream being `upstream`."""
    if session.user_profile is not None:
        await install_tentacle(tentacle, session.user_profile)
    async with upstream_of(upstream) as transport, tentacle.octomate.mcp.lifespan():
        tentacle.octomate.mcp.httpx_client_factory = into(transport)
        server = tentacles_mcp(fixed_session(session), manager=tentacle.octomate.mcp)
        async with Client(server) as client:
            yield client


def an_inkling(
    octomate: Octomate,
    *,
    model: Model | None = None,
    toolsets: Sequence[AbstractToolset[None]] = (),
) -> InklingTentacle:
    """An Inkling on `octomate`, its conversations kept in memory."""
    agent: Agent[None, InklingOutput] = Agent(
        model or TestModel(),
        deps_type=type(None),
        name="octomate-inkling",
        output_type=[str, list[MessageSegment], DeferredToolRequests],
        toolsets=list(toolsets),
    )
    return InklingTentacle(
        "inkling", octomate, agent=agent, conversation_manager=FakeConversationManager()
    )


async def test_an_installed_bare_tentacle_uses_its_operator_credential() -> None:
    tentacle = BareMcpTentacle(
        "linear",
        Octomate(oauth_encryption_key=ENCRYPTION_KEY),
        url=LINEAR_URL,
        token=SecretStr("lin_x"),
    )
    upstream, seen = an_upstream("list_issues")

    owner = await a_user("alice", profiles={"slack": "U1"})
    profile = await tentacle.octomate.users.profile("slack", "U1")
    assert profile is not None
    assert profile.user_id == owner.id
    async with proxied(tentacle, a_turn(profile), upstream) as client:
        tools = await client.list_tools()
        catalog = await discover(client, "personal/linear")
        result = await client.call_tool(
            CALL_MCP_TOOL,
            {"namespace": "personal/linear", "name": "list_issues", "arguments": {}},
        )

    assert {tool.name for tool in tools} == {
        LIST_MCP_TENTACLES,
        LIST_MCPS,
        INSTALL_MCP,
        ENABLE_MCP,
        DISABLE_MCP,
        UNINSTALL_MCP,
        LIST_MCP_TOOLS,
        CALL_MCP_TOOL,
        CONNECT_TOOL,
        CONFIRM_TOOL,
    }
    assert [tool.name for tool in catalog.tools] == ["list_issues"]
    assert result.data == {"result": "answered"}
    assert seen == ["Bearer lin_x"]


async def test_a_linked_person_speaks_with_their_own_token() -> None:
    host, tentacle, profile = await a_linked_host()
    upstream, seen = an_upstream("list_repos")

    owner = await host.users.owner(profile)
    assert owner is not None
    async with proxied(tentacle, a_turn(profile), upstream) as client:
        unlinked = await client.list_tools()
        with pytest.raises(ToolError, match="unavailable"):
            await discover(client, "personal/gh")
        installed = (await host.mcp.list(owner.id))[0]
        await host.mcp.connect(owner, installed.id, profile=profile)
        await host.mcp.confirm(owner, installed.id, profile=profile)
        linked = await client.list_tools()
        catalog = await discover(client, "personal/gh")
        result = await client.call_tool(
            CALL_MCP_TOOL,
            {"namespace": "personal/gh", "name": "list_repos", "arguments": {}},
        )

    # Linking enables discovery without changing the initial tool list.
    assert [tool.name for tool in unlinked] == [tool.name for tool in linked]
    assert [tool.name for tool in linked] == [
        LIST_MCP_TENTACLES,
        LIST_MCPS,
        INSTALL_MCP,
        ENABLE_MCP,
        DISABLE_MCP,
        UNINSTALL_MCP,
        LIST_MCP_TOOLS,
        CALL_MCP_TOOL,
        CONNECT_TOOL,
        CONFIRM_TOOL,
    ]
    assert [tool.name for tool in catalog.tools] == ["list_repos"]
    assert catalog.instructions == tentacle.instructions
    assert result.data == {"result": "answered"}
    assert seen == ["Bearer github-user-token"]


async def test_a_turn_by_nobody_registered_gets_nothing_of_a_persons_provider() -> None:
    _host, tentacle, _profile = await a_linked_host()
    upstream, seen = an_upstream("list_repos")

    async with proxied(tentacle, a_turn(), upstream) as client:
        tools = await client.list_tools()
        with pytest.raises(ToolError, match="unavailable"):
            await client.call_tool(
                CALL_MCP_TOOL,
                {"namespace": "personal/gh", "name": "list_repos", "arguments": {}},
            )

    assert [tool.name for tool in tools] == [
        LIST_MCP_TENTACLES,
        LIST_MCPS,
        INSTALL_MCP,
        ENABLE_MCP,
        DISABLE_MCP,
        UNINSTALL_MCP,
        LIST_MCP_TOOLS,
        CALL_MCP_TOOL,
        CONNECT_TOOL,
        CONFIRM_TOOL,
    ]
    assert seen == []


async def test_inkling_mounts_the_tentacles_deferred_for_the_person_of_its_turn() -> (
    None
):
    _host, tentacle, profile = await a_linked_host()
    ctx = RunContext(deps=None, model=TestModel(), usage=RunUsage())

    capability = tentacles_capability(a_turn(profile), manager=tentacle.octomate.mcp)
    toolset = capability.get_toolset()
    assert toolset is not None
    tools = await toolset.get_tools(ctx)
    instructions = await toolset.get_instructions(ctx)

    # Deferred behind a catalog line, so a listing that differs per person never
    # touches the prompt prefix; before the link, the linking pair and nothing of
    # the provider's, under the served contract.
    assert capability.defer_loading
    assert capability.id == "tentacles"
    assert "user's installed MCP tools" in str(capability.description)
    assert isinstance(instructions, str)
    assert "oauth_connect" in instructions
    assert "The provider's own contract." not in instructions
    assert "`gh` (Provider)" not in instructions
    assert set(tools) == {
        CONNECT_TOOL,
        CONFIRM_TOOL,
        LIST_MCP_TENTACLES,
        LIST_MCPS,
        INSTALL_MCP,
        ENABLE_MCP,
        DISABLE_MCP,
        UNINSTALL_MCP,
        LIST_MCP_TOOLS,
        CALL_MCP_TOOL,
    }


async def test_inkling_calls_a_tentacle_in_process_and_hears_a_refusal_as_a_retry() -> (
    None
):
    host, tentacle, profile = await a_linked_host()
    upstream, seen = an_upstream("list_repos")
    ctx = RunContext(deps=None, model=TestModel(), usage=RunUsage())

    owner = await host.users.owner(profile)
    assert owner is not None
    await install_tentacle(tentacle, profile)
    async with upstream_of(upstream) as transport, host.mcp.lifespan():
        host.mcp.httpx_client_factory = into(transport)
        toolset = TentaclesToolset(
            tentacles_mcp(fixed_session(a_turn(profile)), manager=host.mcp)
        )
        before = await toolset.get_tools(ctx)
        with pytest.raises(ModelRetry, match="Missing required argument"):
            await toolset.call_tool(CONNECT_TOOL, {}, ctx, before[CONNECT_TOOL])
        with pytest.raises(ModelRetry, match="unavailable"):
            await toolset.call_tool(
                LIST_MCP_TOOLS,
                {"namespace": "personal/gh"},
                ctx,
                before[LIST_MCP_TOOLS],
            )
        installed = (await host.mcp.list(owner.id))[0]
        await host.mcp.connect(owner, installed.id, profile=profile)
        await host.mcp.confirm(owner, installed.id, profile=profile)
        after = await toolset.get_tools(ctx)
        answer = await toolset.call_tool(
            CALL_MCP_TOOL,
            {"namespace": "personal/gh", "name": "list_repos", "arguments": {}},
            ctx,
            after[CALL_MCP_TOOL],
        )

    # No client between the run and the server: a refusal is the retry Inkling
    # corrects from, worded as every other runtime reads it; once linked, the
    # call goes as the person and what comes back is what the provider said.
    assert "gh_list_repos" not in before
    assert "gh_list_repos" not in after
    assert answer == "answered"
    assert seen == ["Bearer github-user-token"]


async def test_a_run_mounts_the_tentacles_for_its_octomate_session() -> None:
    host, _tentacle, profile = await a_linked_host()
    offered: list[tuple[dict[str, bool], str | None]] = []

    async def reply(
        messages: list[ModelMessage], info: AgentInfo
    ) -> AsyncIterator[str | DeltaToolCalls]:
        offered.append(
            (
                {tool.name: tool.defer_loading for tool in info.function_tools},
                info.instructions,
            )
        )
        if len(offered) == 1:
            yield {
                0: DeltaToolCall(name="load_capability", json_args='{"id":"tentacles"}')
            }
            return
        yield "noted"

    inkling = an_inkling(
        host, model=FunctionModel(stream_function=reply, model_name="scripted")
    )
    async with inkling.run_stream_events(
        "hello",
        conversation_address=ChannelAddress(
            channel_tentacle_id="slack", chat_type="dm", chat_id="D1", user_id="U1"
        ),
        thread_id=uuid7(),
        capabilities=[GatewayCapability(session=a_turn(profile))],
    ) as stream:
        async for _event in stream:
            pass

    # The model sees the catalog first, then the tools after loading the capability.
    [(tools, instructions), (loaded_tools, _loaded_instructions)] = offered
    assert tools["load_capability"] is False
    assert CONNECT_TOOL not in tools
    assert CONFIRM_TOOL not in tools
    assert CONNECT_TOOL in loaded_tools
    assert CONFIRM_TOOL in loaded_tools
    assert instructions is not None
    assert "- tentacles: The user's installed MCP tools" in instructions


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


async def test_entering_tentacle_enters_agent_toolsets_once() -> None:
    spy = SpyToolset()
    tentacle = an_inkling(Octomate(), toolsets=[spy])

    async with tentacle:
        # Warming runs behind the enter; the warm state is what the task settles.
        assert tentacle.warm_task is not None
        await tentacle.warm_task
        assert (spy.entered, spy.exited) == (1, 0)

    assert (spy.entered, spy.exited) == (1, 1)


async def test_warm_up_failure_does_not_abort_startup() -> None:
    tentacle = an_inkling(Octomate(), toolsets=[FailingToolset()])

    # A transient MCP `initialize` failure while warming must not propagate out of
    # tentacle startup or its background warm task; the agent is left unentered so
    # runs reconnect on demand.
    async with tentacle:
        assert tentacle.warm_task is not None
        await tentacle.warm_task
