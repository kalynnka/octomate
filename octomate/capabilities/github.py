from __future__ import annotations

import logging
from collections.abc import AsyncIterable, Callable
from dataclasses import dataclass, field, replace
from typing import cast

from pydantic import SecretStr
from pydantic_ai import AgentStreamEvent, RunContext
from pydantic_ai.agent.abstract import AgentInstructions
from pydantic_ai.capabilities import AbstractCapability
from pydantic_ai.mcp import MCPToolset
from pydantic_ai.messages import FunctionToolResultEvent, ToolReturn, ToolReturnPart
from pydantic_ai.toolsets import AbstractToolset, FunctionToolset

from octomate.capabilities.harness.events import OAuthAuthorizationEvent
from octomate.capabilities.harness.mcp_cache import McpToolsetCache
from octomate.config import GitHubMcpConfig
from octomate.managers.oauth import OAuthConnector, OAuthManager
from octomate.schemas.oauth import (
    DeviceAuthorization,
    DeviceOAuthFlow,
    OAuthPending,
)
from octomate.schemas.user import UserProfile

# How a channel names this integration when it presents its authorization.
GITHUB_LABEL = "GitHub"

GITHUB_CONNECT_TOOL = "connect_github"
GITHUB_CONFIRM_TOOL = "confirm_github"

# The catalog line the model reads while this capability is deferred — all it knows
# about GitHub until it loads it, so it names both the work and the connecting.
GITHUB_CAPABILITY_DESCRIPTION = (
    "Work with this user's own GitHub account — repositories, issues, pull requests "
    "— and connect that account if they have not linked it yet."
)

GITHUB_OAUTH_INSTRUCTION = """\
## GitHub connection

GitHub tools are unavailable until this user connects their own account.
- Call `connect_github` when the user asks to connect GitHub or needs a GitHub tool.
- It sends the verification link and code directly to this conversation. Do not
  repeat the code in your final response.
- After the user says they authorized it, call `confirm_github` once. If connected,
  GitHub tools become available on their next message.
"""

logger = logging.getLogger(__name__)


@dataclass
class GitHubCapability(AbstractCapability[None]):
    """GitHub OAuth and MCP tools, bound to one registered channel user per run.

    Built once at bootstrap and mounted like any other capability. Unbound it offers a
    run nothing; ``for_profile`` returns the shallow copy that serves one run — OAuth
    tools before that user connects, their own authenticated MCP toolset afterward.
    Everything the copies share stays shared: the registered connector, the MCP server
    settings, and the ``McpToolsetCache`` holding each connected user's warm session,
    which this capability resolves toolsets out of but never keeps.
    """

    manager: OAuthManager
    connector: OAuthConnector
    mcp_config: GitHubMcpConfig = field(default_factory=GitHubMcpConfig)
    # Warm per-user sessions this integration keeps before the least recently used
    # one is closed.
    max_cached_users: int = 32
    cache: McpToolsetCache = field(default_factory=McpToolsetCache, repr=False)
    profile: UserProfile | None = None
    access_token: SecretStr | None = field(default=None, repr=False)
    mcp_toolset: AbstractToolset[None] | None = field(default=None, repr=False)
    mcp_toolset_factory: Callable[[SecretStr], AbstractToolset[None]] | None = field(
        default=None, repr=False
    )
    toolset: AbstractToolset[None] | None = field(default=None, init=False, repr=False)

    def build_mcp_toolset(self, access_token: SecretStr) -> AbstractToolset[None]:
        """Build the authenticated GitHub MCP toolset cached for one user.

        Named after the connector, so the one id a deployment configures reaches the
        MCP session and the prefix its tools carry as well.
        """
        server = self.mcp_config
        url = server.url.rstrip("/") + "/readonly" if server.read_only else server.url
        return (
            MCPToolset(
                url,
                headers={"Authorization": f"Bearer {access_token.get_secret_value()}"},
                id=self.connector.id,
                init_timeout=server.warm_timeout_seconds,
            )
            .prefixed(self.connector.id)
            .defer_loading()
        )

    async def for_profile(self, profile: UserProfile) -> GitHubCapability | None:
        """This run's copy of the capability, bound to the user driving the run.

        `None` when GitHub has nothing to offer this profile — a non-Slack channel, or
        a visitor the deployment never registered. A registered user gets a fresh copy
        every run, since their connection can appear between two messages; what the
        copy shares with this instance is everything else, the warm MCP session
        included — keyed in the cache by the durable user id and rebuilt only when
        their token changes.
        """
        if profile.channel_tentacle_id != "slack":
            return None
        user = await self.manager.users.owner(profile)
        if user is None:
            return None
        access_token = await self.manager.access_token(profile, self.connector.id)
        mcp_toolset: AbstractToolset[None] | None = None
        if access_token is not None:
            build_mcp_toolset = self.mcp_toolset_factory or self.build_mcp_toolset
            mcp_toolset = await self.cache.acquire(
                kind=self.connector.id,
                key=user.id,
                fingerprint=access_token.get_secret_value(),
                max_entries=self.max_cached_users,
                warm_timeout=self.mcp_config.warm_timeout_seconds,
                build=lambda: build_mcp_toolset(access_token),
            )

        return replace(
            self,
            profile=profile,
            access_token=access_token,
            mcp_toolset=mcp_toolset,
        )

    async def __aenter__(self) -> GitHubCapability:
        """Hold every connected user's MCP session open for this capability's lifetime.

        Entered once by the tentacle that mounts it, so the copies serving individual
        runs find a warm session instead of reconnecting.
        """
        await self.cache.__aenter__()
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.cache.__aexit__(*exc)

    def get_description(self) -> str:
        """The catalog line the model reads while this capability is deferred.

        Fixed rather than a field: it is model-facing prose about what GitHub is for,
        not something a deployment composes.
        """
        return GITHUB_CAPABILITY_DESCRIPTION

    def __post_init__(self) -> None:
        if not isinstance(self.connector.flow, DeviceOAuthFlow):
            raise ValueError("GitHubCapability requires device OAuth")
        if self.profile is None:
            return

        if self.access_token is not None:
            self.toolset = self.mcp_toolset or (
                self.mcp_toolset_factory or self.build_mcp_toolset
            )(self.access_token)
            return

        toolset: FunctionToolset[None] = FunctionToolset(id="github-oauth")

        @toolset.tool(name=GITHUB_CONNECT_TOOL)
        async def connect_github(ctx: RunContext[None]) -> ToolReturn[str]:
            """Send this user GitHub's device authorization link and one-time code."""
            if self.profile is None:
                raise RuntimeError("GitHub capability is not bound to a user")
            authorization = await self.manager.start(
                self.profile,
                self.connector.id,
            )
            if not isinstance(authorization, DeviceAuthorization):
                raise ValueError("GitHub is not configured for device OAuth")
            # The link and code go to the channel as an authorization of its own, for
            # the channel to present — never through this return value, which the model
            # reads and could repeat into a reply.
            return ToolReturn(
                return_value=(
                    "The authorization link and code are on their way to this "
                    "conversation."
                ),
                metadata=[
                    OAuthAuthorizationEvent(
                        connector_id=self.connector.id,
                        label=GITHUB_LABEL,
                        verification_uri=str(
                            authorization.verification_uri_complete
                            or authorization.verification_uri
                        ),
                        user_code=authorization.user_code.get_secret_value(),
                    )
                ],
            )

        @toolset.tool(name=GITHUB_CONFIRM_TOOL)
        async def confirm_github(ctx: RunContext[None]) -> str:
            """Confirm that this user authorized their pending GitHub connection."""
            if self.profile is None:
                raise RuntimeError("GitHub capability is not bound to a user")
            result = await self.manager.complete_latest(
                self.profile,
                self.connector.id,
            )
            if isinstance(result, OAuthPending):
                return (
                    "GitHub is still waiting for authorization. Complete the link "
                    f"above, then try again in {result.retry_after_seconds} seconds."
                )
            return (
                f"GitHub connected as @{result.account_label}. GitHub tools will be "
                "available on this user's next message."
            )

        self.toolset = toolset

    def get_toolset(self) -> AbstractToolset[None] | None:
        return self.toolset

    def get_instructions(self) -> AgentInstructions[None] | None:
        if self.profile is not None and self.access_token is None:
            return GITHUB_OAUTH_INSTRUCTION
        return None

    async def wrap_run_event_stream(
        self,
        ctx: RunContext[None],
        *,
        stream: AsyncIterable[AgentStreamEvent],
    ) -> AsyncIterable[AgentStreamEvent]:
        async for event in stream:
            yield event
            if (
                isinstance(event, FunctionToolResultEvent)
                and isinstance(event.part, ToolReturnPart)
                and event.part.tool_name == GITHUB_CONNECT_TOOL
                and isinstance(event.part.metadata, list)
            ):
                for authorization in event.part.metadata:
                    # The tool stashed the OAuthAuthorizationEvent (not an
                    # AgentStreamEvent) in metadata; inject it on the stream for the
                    # channel to present. One dynamic-boundary cast — pydantic-ai types
                    # the stream as AgentStreamEvent, consumers match the concrete
                    # octomate event type.
                    yield cast(AgentStreamEvent, authorization)
