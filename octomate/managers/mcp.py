from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager, suppress
from datetime import UTC, datetime
from typing import NamedTuple

from fastmcp import Client
from fastmcp.client.transports import StreamableHttpTransport
from mcp.shared._httpx_utils import McpHttpClientFactory
from sqlalchemy.exc import IntegrityError
from uuid_utils.compat import uuid7

from octomate.config.mcp.pool import McpPoolConfig
from octomate.database import async_session
from octomate.managers.base import Locks, Manager
from octomate.managers.gateway import OctomateSession
from octomate.managers.user import UserManager
from octomate.mcp.transport import mcp_http_client
from octomate.schemas.mcp import (
    BearerAuth,
    BearerMcp,
    Mcp,
    McpInstallRequest,
    NoAuth,
    NoAuthMcp,
    OAuth,
    OAuthMcp,
)
from octomate.schemas.oauth import OAuthCipher

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
        config: McpPoolConfig | None = None,
        httpx_client_factory: McpHttpClientFactory = mcp_http_client,
    ) -> None:
        self.users: UserManager = users
        self.cipher: OAuthCipher | None = cipher
        self.config: McpPoolConfig = config if config is not None else McpPoolConfig()
        self.httpx_client_factory: McpHttpClientFactory = httpx_client_factory
        self.clients: dict[McpClientKey, Client] = {}
        self.sweaps: dict[McpClientKey, asyncio.Task[None]] = {}

    async def install(self, user_id: uuid.UUID, request: McpInstallRequest) -> Mcp:
        mcp_id = uuid7()
        namespace = f"personal/{request.namespace}"
        instance: Mcp
        match request.auth:
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
                    encrypted_token=self.cipher.encrypt(
                        request.auth.token.get_secret_value(),
                        context=f"mcp:{user_id}:{mcp_id}",
                    ),
                )
            case OAuth():
                raise ValueError("OAuth MCP authentication is not implemented yet")
            case NoAuth():
                instance = NoAuthMcp(
                    id=mcp_id,
                    user_id=user_id,
                    name=request.name,
                    namespace=namespace,
                    url=str(request.url),
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
                await session.delete(instance)
                await session.commit()
            await self.evict(McpClientKey(user_id=user_id, mcp_id=mcp_id))

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
            if isinstance(current, OAuthMcp):
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
        await asyncio.sleep(self.config.idle_timeout)
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
