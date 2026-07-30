"""Per-user MCP: the warm session cache, and the capability that spends one.

Both halves of the same idea, so they live together. A `McpToolsetCache` keeps one
authenticated session per user per integration alive between agent runs, and
`OAuthMcpCapability` is what resolves a run's toolset out of it — offering an
invitation to authorize when there is no credential yet, and that user's own tools
once there is. Neither is provider-specific; a concrete integration supplies only
which flow its connector composes, what its tools are called, and the prose the
model reads.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections import OrderedDict
from collections.abc import AsyncIterable, Callable
from contextlib import AsyncExitStack
from dataclasses import dataclass, field, replace
from typing import ClassVar, Self, cast

from pydantic import SecretStr
from pydantic_ai import AgentStreamEvent, RunContext
from pydantic_ai.agent.abstract import AgentInstructions
from pydantic_ai.capabilities import AbstractCapability
from pydantic_ai.mcp import MCPToolset
from pydantic_ai.messages import FunctionToolResultEvent, ToolReturnPart
from pydantic_ai.toolsets import AbstractToolset, FunctionToolset

from octomate.config.mcp import McpIntegrationConfig
from octomate.managers.oauth import OAuthConnector, OAuthManager
from octomate.oauth.base import McpConnectionAuth
from octomate.schemas.oauth import AuthorizationCodeOAuthFlow, DeviceOAuthFlow
from octomate.schemas.user import UserProfile

logger = logging.getLogger(__name__)


@dataclass
class McpCacheEntry:
    # The credential identity this session was built for; a change rebuilds it. Kept
    # out of repr so a token never lands in a log or traceback.
    fingerprint: str = field(repr=False)
    toolset: AbstractToolset[None]
    # Seconds this session's owning integration allows for warming — its own override
    # or the general default, resolved by the caller.
    warm_timeout: float
    exit_stack: AsyncExitStack = field(default_factory=AsyncExitStack, repr=False)
    warmed: bool = False


class McpToolsetCache:
    """Bounded, warm per-user MCP toolset cache shared across agent runs.

    A user's authenticated MCP session must never serve another user or another
    credential: swapping the bearer token would race concurrent runs, and the cached
    ``tools/list`` can differ by granted scope. So every ``(kind, key)`` — keyed by the
    durable user id — gets its own toolset and its own warm session.

    Each kind keeps an independent LRU bounded by the ``max_entries`` its caller
    supplies, and each entry warms under the ``warm_timeout`` its caller supplies — both
    the owning integration's own settings. Admitting one past the bound evicts and closes
    the least-recently-used session for that kind, and a changed fingerprint closes the
    stale session before rebuilding. Entering the cache keeps every live session warm for
    its lifetime; exiting closes them all.
    """

    def __init__(self) -> None:
        self.kinds: dict[str, OrderedDict[uuid.UUID, McpCacheEntry]] = {}
        self.lock = asyncio.Lock()
        self.active = False

    async def acquire(
        self,
        *,
        kind: str,
        key: uuid.UUID,
        fingerprint: str,
        max_entries: int,
        warm_timeout: float,
        build: Callable[[], AbstractToolset[None]],
    ) -> AbstractToolset[None]:
        """Return the warm toolset for ``(kind, key)``, building it if needed.

        Reuses the cached session when the fingerprint is unchanged; otherwise closes
        the stale one and builds a fresh session for the new credential. ``max_entries``
        bounds this kind's LRU and ``warm_timeout`` caps its warming — both the owning
        integration's own settings.
        """
        async with self.lock:
            entries = self.kinds.setdefault(kind, OrderedDict())
            entry = entries.get(key)
            if entry is not None and entry.fingerprint == fingerprint:
                entries.move_to_end(key)
            else:
                if entry is not None:
                    await entry.exit_stack.aclose()
                entry = McpCacheEntry(
                    fingerprint=fingerprint, toolset=build(), warm_timeout=warm_timeout
                )
                entries[key] = entry
                while len(entries) > max_entries:
                    _, evicted = entries.popitem(last=False)
                    await evicted.exit_stack.aclose()
            if self.active:
                await self.warm(entry)
            return entry.toolset

    async def warm(self, entry: McpCacheEntry) -> None:
        """Keep a session and its `tools/list` cache alive between agent runs.

        Holding one reference in the entry's own exit stack pins the session open; the
        run enters the same toolset again (reference-counted) and the priming call means
        the first run does not block on a multi-second `tools/list`.
        """
        if entry.warmed:
            return
        try:
            await asyncio.wait_for(
                entry.exit_stack.enter_async_context(entry.toolset),
                timeout=entry.warm_timeout,
            )
        except Exception:
            logger.warning(
                "Failed to warm MCP session; the agent run will retry",
                exc_info=True,
            )
            return
        entry.warmed = True

        mcp_servers: list[MCPToolset[None]] = []

        def collect(candidate: AbstractToolset[None]) -> None:
            if isinstance(candidate, MCPToolset):
                mcp_servers.append(candidate)

        entry.toolset.apply(collect)
        if mcp_servers:
            try:
                await asyncio.wait_for(
                    asyncio.gather(*(server.list_tools() for server in mcp_servers)),
                    timeout=entry.warm_timeout,
                )
            except Exception:
                logger.warning(
                    "Failed to prime MCP tools; the agent run will retry",
                    exc_info=True,
                )

    async def __aenter__(self) -> McpToolsetCache:
        self.active = True
        async with self.lock:
            for entries in self.kinds.values():
                for entry in entries.values():
                    await self.warm(entry)
        return self

    async def __aexit__(self, *exc: object) -> None:
        self.active = False
        async with self.lock:
            for entries in self.kinds.values():
                for entry in entries.values():
                    await entry.exit_stack.aclose()
            self.kinds.clear()


@dataclass
class OAuthMcpCapability(AbstractCapability[None]):
    """A provider's OAuth and MCP tools, bound to one registered channel user per run.

    Built once at bootstrap and mounted like any other capability. Unbound it offers a
    run nothing; ``for_profile`` returns the shallow copy that serves one run — OAuth
    tools before that user connects, their own authenticated MCP toolset afterward.
    Everything the copies share stays shared: the registered connector, the MCP server
    settings, and the ``McpToolsetCache`` holding each connected user's warm session,
    which this capability resolves toolsets out of but never keeps.
    """

    manager: OAuthManager
    connector: OAuthConnector
    mcp_config: McpIntegrationConfig
    # Warm per-user sessions this integration keeps before the least recently used
    # one is closed.
    max_cached_users: int = 32
    cache: McpToolsetCache = field(default_factory=McpToolsetCache, repr=False)
    profile: UserProfile | None = None
    access_token: SecretStr | None = field(default=None, repr=False)
    mcp_toolset: AbstractToolset[None] | None = field(default=None, repr=False)
    # This user connected once and the provider has since rejected the credential,
    # which reads as unconnected everywhere except in what the model is told.
    connection_retired: bool = False
    mcp_toolset_factory: (
        Callable[[UserProfile, SecretStr], AbstractToolset[None]] | None
    ) = field(default=None, repr=False)
    toolset: AbstractToolset[None] | None = field(default=None, init=False, repr=False)

    # What a concrete integration owes this one. Declared without defaults so a
    # subclass that forgets one fails on the attribute rather than serving the
    # model an empty string.
    label: ClassVar[str]
    connect_tool: ClassVar[str]
    confirm_tool: ClassVar[str]
    capability_description: ClassVar[str]
    oauth_instruction: ClassVar[str]
    retired_instruction: ClassVar[str]
    flow_type: ClassVar[type[DeviceOAuthFlow] | type[AuthorizationCodeOAuthFlow]]

    def build_oauth_toolset(self) -> FunctionToolset[None]:
        """The tools offered to a registered user who has not connected yet.

        The one thing an integration cannot inherit: what a user must do to authorize
        is the difference between a device code and a browser round trip.
        """
        raise NotImplementedError

    def build_mcp_toolset(
        self,
        profile: UserProfile,
        access_token: SecretStr,
    ) -> AbstractToolset[None]:
        """Build the authenticated MCP toolset cached for one user.

        The session is named after the connector, so the one id a deployment
        configures reaches it too. Its tools carry that name as well unless the server
        overrides it: the connector id is durable — it keys stored connections — while
        the prefix is only what the model reads, and one vendor mounted twice needs
        those to be able to differ. The profile is whose connection this session
        spends, and so whose connection a 401 retires.
        """
        server = self.mcp_config
        url = server.url.rstrip("/") + "/readonly" if server.read_only else server.url
        return (
            MCPToolset(
                url,
                auth=McpConnectionAuth(
                    access_token,
                    lambda: self.manager.invalidate(profile, self.connector.id),
                ),
                id=self.connector.id,
                init_timeout=server.warm_timeout_seconds,
            )
            .prefixed(server.prefix or self.connector.id)
            .defer_loading()
        )

    async def for_profile(self, profile: UserProfile) -> Self | None:
        """This run's copy of the capability, bound to the user driving the run.

        `None` when this integration has nothing to offer this profile — a visitor the
        deployment never registered. The channel does not matter: the `users:`
        registry is the authority on who is a real human, and every channel can
        present an authorization, with a card where the platform has them.

        A registered user gets a fresh copy every run, since their connection can
        appear between two messages; what the copy shares with this instance is
        everything else, the warm MCP session included — keyed in the cache by the
        durable user id and rebuilt only when their token changes.
        """
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
                build=lambda: build_mcp_toolset(profile, access_token),
            )

        return replace(
            self,
            profile=profile,
            access_token=access_token,
            mcp_toolset=mcp_toolset,
            # Only worth asking when there is no token to explain: a connection that
            # works is its own explanation.
            connection_retired=access_token is None
            and await self.manager.connection_status(profile, self.connector.id)
            == "invalid",
        )

    async def __aenter__(self) -> Self:
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

        Fixed rather than a field: it is model-facing prose about what the provider is
        for, not something a deployment composes.
        """
        return self.capability_description

    def __post_init__(self) -> None:
        if not isinstance(self.connector.flow, self.flow_type):
            raise ValueError(
                f"{type(self).__name__} requires {self.flow_type.__name__}"
            )
        if self.profile is None:
            return

        if self.access_token is not None:
            self.toolset = self.mcp_toolset or (
                self.mcp_toolset_factory or self.build_mcp_toolset
            )(self.profile, self.access_token)
            return

        self.toolset = self.build_oauth_toolset()

    def bound_profile(self) -> UserProfile:
        """The user this run is answering, for a tool that only exists once bound."""
        if self.profile is None:
            raise RuntimeError(f"{type(self).__name__} is not bound to a user")
        return self.profile

    def get_toolset(self) -> AbstractToolset[None] | None:
        return self.toolset

    def get_instructions(self) -> AgentInstructions[None] | None:
        if self.profile is None or self.access_token is not None:
            return None
        if self.connection_retired:
            return self.retired_instruction
        return self.oauth_instruction

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
                and event.part.tool_name == self.connect_tool
                and isinstance(event.part.metadata, list)
            ):
                for authorization in event.part.metadata:
                    # The tool stashed the OAuthAuthorizationEvent (not an
                    # AgentStreamEvent) in metadata; inject it on the stream for the
                    # channel to present. One dynamic-boundary cast — pydantic-ai types
                    # the stream as AgentStreamEvent, consumers match the concrete
                    # octomate event type.
                    yield cast(AgentStreamEvent, authorization)
