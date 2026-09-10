from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager, nullcontext, suppress
from datetime import UTC, datetime
from typing import TYPE_CHECKING, NamedTuple

from fastmcp import Client
from fastmcp.client.transports import StreamableHttpTransport
from mcp.shared._httpx_utils import McpHttpClientFactory
from sqlalchemy.exc import IntegrityError
from uuid_utils.compat import uuid7

from octomate.config.mcp.pool import McpPoolConfig
from octomate.database import async_session
from octomate.managers.base import Locks, Manager
from octomate.managers.gateway import OctomateSession
from octomate.managers.oauth import NoPendingAuthorization, OAuthLockKey, OAuthManager
from octomate.managers.user import UserManager
from octomate.mcp.transport import mcp_http_client
from octomate.oauth.base import McpBearerAuth
from octomate.schemas.mcp import (
    BearerAuth,
    BearerMcp,
    Mcp,
    McpAuthorizationResult,
    McpAuthorizationStatus,
    McpBrowserAuthorizationPending,
    McpDeviceAuthorizationPending,
    McpInstallRequest,
    McpTentacleInfo,
    McpToolCatalog,
    NoAuth,
    NoAuthMcp,
    OAuth,
    OAuthMcp,
)
from octomate.schemas.oauth import (
    DeviceOAuthFlow,
    OAuthCipher,
    OAuthOperation,
    OAuthPending,
    OAuthStartResult,
)
from octomate.schemas.user import User, UserProfile
from octomate.types.oauth import OAuthFlowKind

if TYPE_CHECKING:
    from octomate.tentacles.mcp import McpTentacle

logger = logging.getLogger(__name__)


class McpUnavailable(LookupError):
    def __init__(self) -> None:
        super().__init__("MCP instance unavailable")


class McpClientKey(NamedTuple):
    user_id: uuid.UUID
    mcp_id: uuid.UUID


class McpManager(Manager, Locks[uuid.UUID]):
    def __init__(
        self,
        users: UserManager,
        cipher: OAuthCipher | None,
        *,
        idle_timeout: float | None = None,
        httpx_client_factory: McpHttpClientFactory = mcp_http_client,
        oauth: OAuthManager | None = None,
    ) -> None:
        self.users: UserManager = users
        self.cipher: OAuthCipher | None = cipher
        self.idle_timeout: float = (
            idle_timeout if idle_timeout is not None else McpPoolConfig().idle_timeout
        )
        self.httpx_client_factory: McpHttpClientFactory = httpx_client_factory
        self.tentacles: dict[str, McpTentacle] = {}
        self.oauth = oauth
        self.clients: dict[McpClientKey, Client] = {}
        self.sweaps: dict[McpClientKey, asyncio.Task[None]] = {}

    def available(self) -> list[McpTentacleInfo]:
        return [
            tentacle.info for tentacle in self.tentacles.values() if tentacle.serving
        ]

    async def install(self, user_id: uuid.UUID, request: McpInstallRequest) -> Mcp:
        mcp_id = uuid7()
        namespace = f"personal/{request.namespace}"
        instance: Mcp
        tentacle_id = request.tentacle_id
        auth = request.auth or NoAuth()
        instructions = ""
        if tentacle_id is not None:
            tentacle = self.tentacles.get(tentacle_id)
            if tentacle is None or not tentacle.serving:
                raise McpUnavailable
            if tentacle.upstream != str(request.url):
                raise ValueError("MCP URL must match the tentacle")
            instructions = tentacle.instructions
            if tentacle.auth_kind == "bearer":
                if tentacle.token is None:
                    raise ValueError("Bearer MCPs require a token")
                auth = BearerAuth(token=tentacle.token)
            elif tentacle.auth_kind == "oauth":
                auth = OAuth()
        match auth:
            case BearerAuth():
                if self.cipher is None:
                    raise ValueError(
                        "Configure oauth.encryption_key before installing bearer MCPs"
                    )
                instance = BearerMcp(
                    id=mcp_id,
                    user_id=user_id,
                    name=request.name,
                    namespace=namespace,
                    url=str(request.url),
                    instructions=instructions,
                    tentacle_id=tentacle_id,
                    encrypted_token=self.cipher.encrypt(
                        auth.token.get_secret_value(),
                        context=f"mcp:{user_id}:{mcp_id}",
                    ),
                )
            case OAuth():
                if self.cipher is None or self.oauth is None:
                    raise ValueError(
                        "Configure oauth.encryption_key before installing OAuth MCPs"
                    )
                if tentacle_id is not None:
                    connector = self.oauth.connector(tentacle_id)
                    if connector.mcp_url is None or str(connector.mcp_url) != str(
                        request.url
                    ):
                        raise ValueError(
                            "MCP URL must match the configured OAuth tentacle"
                        )
                elif self.oauth.callback_base_uri is None:
                    raise ValueError(
                        "Configure oauth.callback_base_uri before installing dynamic OAuth MCPs"
                    )
                instance = OAuthMcp(
                    id=mcp_id,
                    user_id=user_id,
                    name=request.name,
                    namespace=namespace,
                    url=str(request.url),
                    instructions=instructions,
                    tentacle_id=tentacle_id,
                )
            case NoAuth():
                instance = NoAuthMcp(
                    id=mcp_id,
                    user_id=user_id,
                    name=request.name,
                    namespace=namespace,
                    url=str(request.url),
                    instructions=instructions,
                    tentacle_id=tentacle_id,
                )
        async with async_session() as session:
            session.add(instance)
            try:
                await session.commit()
            except IntegrityError as error:
                await session.rollback()
                duplicate = await session.one_or_none(
                    Mcp,
                    expressions=[
                        Mcp["user_id"] == user_id,
                        Mcp["namespace"] == namespace,
                    ],
                )
                if duplicate is None:
                    raise
                raise ValueError("MCP namespace is already installed") from error
        return instance

    async def list(self, user_id: uuid.UUID, *, enabled: bool = False) -> list[Mcp]:
        expressions = [Mcp["user_id"] == user_id]
        if enabled:
            expressions.append(Mcp["enabled"].is_(True))
        async with async_session() as session:
            return list(await session.list(Mcp, expressions=expressions, limit=None))

    async def enable(self, user_id: uuid.UUID, mcp_id: uuid.UUID) -> Mcp:
        async with self.lock(mcp_id):
            async with async_session() as session:
                instance = await session.one_or_none(
                    Mcp,
                    expressions=[
                        Mcp["id"] == mcp_id,
                        Mcp["user_id"] == user_id,
                    ],
                )
                if instance is None:
                    raise McpUnavailable
                instance.enabled = True
                instance.updated_at = datetime.now(UTC)
                await session.commit()
            return instance

    async def disable(self, user_id: uuid.UUID, mcp_id: uuid.UUID) -> Mcp:
        async with self.lock(mcp_id):
            async with async_session() as session:
                instance = await session.one_or_none(
                    Mcp,
                    expressions=[
                        Mcp["id"] == mcp_id,
                        Mcp["user_id"] == user_id,
                    ],
                )
                if instance is None:
                    raise McpUnavailable
                async with (
                    self.oauth.lock(
                        OAuthLockKey(
                            user_id=user_id,
                            mcp_id=mcp_id,
                            connector_id=instance.tentacle_id or "mcp",
                        )
                    )
                    if isinstance(instance, OAuthMcp) and self.oauth is not None
                    else nullcontext()
                ):
                    instance.enabled = False
                    instance.updated_at = datetime.now(UTC)
                    await session.commit()
            await self.evict(McpClientKey(user_id=user_id, mcp_id=mcp_id))
            return instance

    async def uninstall(self, user_id: uuid.UUID, mcp_id: uuid.UUID) -> None:
        async with self.lock(mcp_id):
            async with async_session() as session:
                instance = await session.one_or_none(
                    Mcp,
                    expressions=[
                        Mcp["id"] == mcp_id,
                        Mcp["user_id"] == user_id,
                    ],
                )
                if instance is None:
                    raise McpUnavailable
                async with (
                    self.oauth.lock(
                        OAuthLockKey(
                            user_id=user_id,
                            mcp_id=mcp_id,
                            connector_id=instance.tentacle_id or "mcp",
                        )
                    )
                    if isinstance(instance, OAuthMcp) and self.oauth is not None
                    else nullcontext()
                ):
                    await session.delete(instance)
                    await session.commit()
            await self.evict(McpClientKey(user_id=user_id, mcp_id=mcp_id))

    async def authorizable(
        self,
        user_id: uuid.UUID,
        *,
        mcp_id: uuid.UUID | None = None,
        namespace: str | None = None,
    ) -> OAuthMcp:
        if mcp_id is None and namespace is None:
            raise ValueError("MCP ID or namespace is required")
        expressions = [
            OAuthMcp["user_id"] == user_id,
            OAuthMcp["enabled"].is_(True),
        ]
        if mcp_id is not None:
            expressions.append(OAuthMcp["id"] == mcp_id)
        if namespace is not None:
            expressions.append(OAuthMcp["namespace"] == namespace)
        async with async_session() as session:
            instance = await session.one_or_none(OAuthMcp, expressions=expressions)
        if instance is None:
            raise McpUnavailable
        return instance

    async def connect(
        self,
        user: User,
        mcp_id: uuid.UUID,
        *,
        profile: UserProfile | None = None,
        flow: OAuthFlowKind | None = None,
    ) -> OAuthStartResult:
        if self.oauth is None:
            raise McpUnavailable
        instance = await self.authorizable(user_id=user.id, mcp_id=mcp_id)
        async with self.oauth.lock(
            OAuthLockKey(
                user_id=user.id,
                mcp_id=mcp_id,
                connector_id=instance.tentacle_id or "mcp",
            )
        ):
            return await self.oauth.start(
                user,
                instance.tentacle_id or "mcp",
                mcp_id=mcp_id,
                profile=profile,
                flow=flow,
            )

    async def confirm(
        self,
        user: User,
        mcp_id: uuid.UUID,
        *,
        profile: UserProfile | None = None,
    ) -> McpAuthorizationResult:
        if self.oauth is None:
            raise McpUnavailable
        instance = await self.authorizable(user_id=user.id, mcp_id=mcp_id)
        connector_id = instance.tentacle_id or "mcp"
        connector = await self.oauth.resolve_connector(
            connector_id, user_id=user.id, mcp_id=mcp_id
        )
        if any(isinstance(flow, DeviceOAuthFlow) for flow in connector.flows):
            try:
                result = await self.oauth.complete_latest(
                    user, connector_id, mcp_id=mcp_id, profile=profile
                )
            except NoPendingAuthorization:
                pass
            else:
                if isinstance(result, OAuthPending):
                    return McpDeviceAuthorizationPending(
                        retry_after_seconds=result.retry_after_seconds
                    )
        status = await self.oauth.connection_status(user, connector_id, mcp_id=mcp_id)
        if status is None:
            async with async_session() as session:
                pending = await session.first(
                    OAuthOperation,
                    expressions=[
                        OAuthOperation["user_id"] == user.id,
                        OAuthOperation["mcp_id"] == mcp_id,
                        OAuthOperation["consumed_at"].is_(None),
                        OAuthOperation["expires_at"] > datetime.now(UTC),
                    ],
                )
            if pending is not None:
                return McpBrowserAuthorizationPending()
        return McpAuthorizationStatus(status=status)

    async def catalog(self, scope: OctomateSession, namespace: str) -> McpToolCatalog:
        async with self.acquire(scope, namespace) as client:
            assert scope.user_profile is not None
            user = await self.users.owner(scope.user_profile)
            if user is None:
                raise McpUnavailable
            async with async_session() as session:
                instance = await session.one_or_none(
                    Mcp,
                    expressions=[
                        Mcp["user_id"] == user.id,
                        Mcp["namespace"] == namespace,
                    ],
                )
            if instance is None:
                raise McpUnavailable
            instructions = "\n".join(
                part for part in (instance.instructions, client.instructions) if part
            )
            return McpToolCatalog(
                instructions=instructions, tools=await client.list_tools()
            )

    @asynccontextmanager
    async def acquire(
        self, scope: OctomateSession, namespace: str
    ) -> AsyncGenerator[Client]:
        if scope.user_profile is None:
            raise McpUnavailable
        user = await self.users.owner(scope.user_profile)
        if user is None:
            raise McpUnavailable
        user_id = user.id
        async with async_session() as session:
            instance = await session.one_or_none(
                Mcp,
                expressions=[
                    Mcp["user_id"] == user_id,
                    Mcp["namespace"] == namespace,
                    Mcp["enabled"].is_(True),
                ],
            )
        if instance is None:
            raise McpUnavailable
        async with self.lock(instance.id):
            # The first lookup finds the lock; reload under it so disabling or
            # deleting an instance wins before another call is dispatched.
            async with async_session() as session:
                current = await session.one_or_none(
                    Mcp,
                    expressions=[Mcp["id"] == instance.id],
                )
            if current is None or not current.enabled or current.user_id != user_id:
                raise McpUnavailable
            if isinstance(current, OAuthMcp) and (
                self.oauth is None
                or await self.oauth.connection_status(
                    user, current.tentacle_id or "mcp", mcp_id=current.id
                )
                != "active"
            ):
                raise McpUnavailable
            key = McpClientKey(user_id=user_id, mcp_id=current.id)
            client = self.clients.get(key)
            if client is not None and not client.is_connected():
                await self.evict(key)
                client = None
            if client is None:
                headers: dict[str, str] = {}
                if isinstance(current, BearerMcp):
                    if self.cipher is None:
                        raise ValueError("MCP credential encryption is not configured")
                    token = self.cipher.decrypt(
                        current.encrypted_token, context=f"mcp:{user_id}:{current.id}"
                    )
                    headers["Authorization"] = f"Bearer {token}"
                client = Client(
                    transport=StreamableHttpTransport(
                        url=current.url,
                        headers=headers,
                        auth=McpBearerAuth(
                            manager=self.oauth,
                            user=user,
                            mcp_id=current.id,
                            connector_id=current.tentacle_id or "mcp",
                            url=current.url,
                        )
                        if isinstance(current, OAuthMcp) and self.oauth is not None
                        else None,
                        httpx_client_factory=self.httpx_client_factory,
                    ),
                    mode="2026-07-28",
                    cache=False,
                )
                await client.__aenter__()
                self.clients[key] = client
            # Acquire the call's reference before releasing the lock, so removal
            # cannot close the client between checking access and starting a call.
            try:
                await client.__aenter__()
            except Exception:
                if not client.is_connected():
                    await self.evict(key)
                raise
            if cleanup := self.sweaps.pop(key, None):
                cleanup.cancel()
                with suppress(asyncio.CancelledError):
                    await cleanup
            self.sweaps[key] = asyncio.create_task(self.expire(key))
        try:
            yield client
        finally:
            try:
                await client.__aexit__(None, None, None)
            finally:
                if not client.is_connected():
                    async with self.lock(key.mcp_id):
                        if self.clients.get(key) is client:
                            await self.evict(key)

    async def expire(self, key: McpClientKey) -> None:
        await asyncio.sleep(self.idle_timeout)
        async with self.lock(key.mcp_id):
            await self.evict(key)

    async def evict(self, key: McpClientKey) -> None:
        """Close a cached client while the caller holds its MCP lock."""
        if cleanup := self.sweaps.pop(key, None):
            if cleanup is not asyncio.current_task():
                cleanup.cancel()
                with suppress(asyncio.CancelledError):
                    await cleanup
        if client := self.clients.get(key):
            try:
                await client.close()
            except Exception:
                logger.exception("Failed to close MCP client %s", key.mcp_id)
            finally:
                del self.clients[key]

    @asynccontextmanager
    async def lifespan(self) -> AsyncGenerator[None]:
        try:
            yield
        finally:
            while self.clients:
                key = next(iter(self.clients))
                async with self.lock(key.mcp_id):
                    await self.evict(key)
