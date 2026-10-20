"""Per-user MCP installations: install, enable and authorize them, and pool their
clients."""

from __future__ import annotations

import asyncio
import logging
from base64 import b64decode
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager, nullcontext, suppress
from datetime import UTC, datetime
from typing import TYPE_CHECKING, NamedTuple

from cachetools import LRUCache
from fastmcp import Client
from fastmcp.apps.config import UI_EXTENSION_ID, UI_MIME_TYPE
from fastmcp.client.transports import StreamableHttpTransport
from mcp.client.extension import advertise
from mcp.shared._httpx_utils import McpHttpClientFactory
from mcp.types import CallToolResult, TextResourceContents
from pydantic import UUID7, JsonValue
from sqlalchemy.exc import IntegrityError
from uuid_utils.compat import uuid7

from octomate.capabilities.harness.events import ArtifactAvailableEvent
from octomate.config.mcp.pool import McpPoolConfig
from octomate.database import async_session
from octomate.managers.base import Locks, Manager
from octomate.managers.gateway import OctomateSession
from octomate.managers.oauth import NoPendingAuthorization, OAuthLockKey, OAuthManager
from octomate.managers.user import UserManager
from octomate.mcp.transport import mcp_http_client
from octomate.oauth.base import McpBearerAuth
from octomate.oauth.flows import DeviceAuthorizationFlow
from octomate.schemas.artifacts import InstalledMcpAppSource, McpAppArtifact
from octomate.schemas.mcp import (
    BearerAuth,
    BearerMcp,
    Mcp,
    McpAppInvocation,
    McpAppResource,
    McpAppToolMeta,
    McpAuthorizationResult,
    McpAuthorizationStatus,
    McpBrowserAuthorizationPending,
    McpDeviceAuthorizationPending,
    McpInstallRequest,
    McpOAuthSummary,
    McpServerSummary,
    McpTentacleInfo,
    McpToolCatalog,
    NoAuth,
    NoAuthMcp,
    OAuth,
    OAuthMcp,
)
from octomate.schemas.oauth import (
    OAuthCipher,
    OAuthOperation,
    OAuthPending,
    OAuthStartResult,
)
from octomate.schemas.user import User, UserProfile
from octomate.tentacles.lark import LarkTentacle
from octomate.tentacles.lark.oauth import LarkMcpBearerAuth
from octomate.types.oauth import OAuthFlowKind

if TYPE_CHECKING:
    from octomate.tentacles.mcp import McpTentacle

logger = logging.getLogger(__name__)


class McpUnavailable(LookupError):
    """The MCP instance or tentacle named is unknown, not the caller's, or not
    serving."""

    def __init__(self) -> None:
        super().__init__("MCP instance unavailable")


class McpClientKey(NamedTuple):
    """The owner and installation a pooled client is cached under."""

    user_id: UUID7
    mcp_id: UUID7


class McpManager(Manager, Locks[UUID7]):
    """A user's MCP installations: install, enable, authorize and remove them, and
    pool their clients.

    All connections advertise MCP Apps support. Client acquisition and capability
    negotiation are independent of channel rendering and sandbox configuration.
    """

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
        # Latest discovery per pooled connection, bounded by least-recent use.
        self.tool_catalogs: LRUCache[McpClientKey, McpToolCatalog] = LRUCache(
            maxsize=256
        )
        self.sweaps: dict[McpClientKey, asyncio.Task[None]] = {}

    def available(self) -> list[McpTentacleInfo]:
        return [
            tentacle.info for tentacle in self.tentacles.values() if tentacle.serving
        ]

    async def install(self, user_id: UUID7, request: McpInstallRequest) -> Mcp:
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

    async def list[McpT: Mcp](
        self,
        user_id: UUID7,
        *,
        enabled: bool = False,
        mcp_type: type[McpT] = Mcp,
    ) -> list[McpT]:
        expressions = [Mcp["user_id"] == user_id]
        if enabled:
            expressions.append(Mcp["enabled"].is_(True))
        async with async_session() as session:
            return list(
                await session.list(
                    mcp_type,
                    expressions=expressions,
                    limit=None,
                )
            )

    async def summary(self, user: User, mcp: Mcp) -> McpServerSummary:
        if mcp.user_id != user.id:
            raise McpUnavailable
        summary = mcp.summary
        if isinstance(mcp, OAuthMcp):
            authorization: McpAuthorizationResult = McpAuthorizationStatus(status=None)
            flows: list[OAuthFlowKind] = []
            if self.oauth is not None:
                authorization = await self.authorization_status(user, mcp)
                if mcp.tentacle_id is None:
                    if self.oauth.callback_base_uri is not None:
                        flows = ["authorization_code"]
                else:
                    connector = self.oauth.connectors.get(mcp.tentacle_id)
                    if connector is not None:
                        flows = [flow.kind for flow in connector.flows]
            summary.oauth = McpOAuthSummary(status=authorization.status, flows=flows)
        return summary

    async def enable(self, user_id: UUID7, mcp_id: UUID7) -> Mcp:
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

    async def disable(self, user_id: UUID7, mcp_id: UUID7) -> Mcp:
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

    async def uninstall(self, user_id: UUID7, mcp_id: UUID7) -> None:
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
        user_id: UUID7,
        *,
        mcp_id: UUID7 | None = None,
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
        mcp_id: UUID7,
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

    async def pending_authorization(
        self, user: User, mcp_id: UUID7
    ) -> OAuthStartResult | None:
        if self.oauth is None:
            raise McpUnavailable
        mcp = await self.authorizable(user_id=user.id, mcp_id=mcp_id)
        return await self.oauth.pending_authorization(
            user, mcp.tentacle_id or "mcp", mcp_id=mcp_id
        )

    async def cancel_authorization(self, user: User, mcp_id: UUID7) -> None:
        if self.oauth is None:
            raise McpUnavailable
        mcp = await self.authorizable(user_id=user.id, mcp_id=mcp_id)
        connector_id = mcp.tentacle_id or "mcp"
        async with (
            self.oauth.lock(
                OAuthLockKey(user_id=user.id, mcp_id=mcp_id, connector_id=connector_id)
            ),
            async_session() as session,
        ):
            operations = await session.list(
                OAuthOperation,
                limit=None,
                expressions=[
                    OAuthOperation["user_id"] == user.id,
                    OAuthOperation["mcp_id"] == mcp_id,
                    OAuthOperation["connector_id"] == connector_id,
                    OAuthOperation["consumed_at"].is_(None),
                ],
            )
            now = datetime.now(UTC)
            for operation in operations:
                operation.consumed_at = now
            await session.commit()

    async def confirm(
        self,
        user: User,
        mcp_id: UUID7,
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
        if any(isinstance(flow, DeviceAuthorizationFlow) for flow in connector.flows):
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
        return await self.authorization_status(user, instance)

    async def authorization_status(
        self, user: User, mcp: OAuthMcp
    ) -> McpAuthorizationResult:
        if self.oauth is None or mcp.user_id != user.id:
            raise McpUnavailable
        connector_id = mcp.tentacle_id or "mcp"
        status = await self.oauth.connection_status(user, connector_id, mcp_id=mcp.id)
        if status != "active":
            async with async_session() as session:
                pending = await session.first(
                    OAuthOperation,
                    expressions=[
                        OAuthOperation["user_id"] == user.id,
                        OAuthOperation["mcp_id"] == mcp.id,
                        OAuthOperation["connector_id"] == connector_id,
                        OAuthOperation["consumed_at"].is_(None),
                        OAuthOperation["expires_at"] > datetime.now(UTC),
                    ],
                    order_bys=[OAuthOperation["id"].desc()],
                )
            if pending is not None:
                if pending.interval_seconds is not None:
                    return McpDeviceAuthorizationPending(
                        retry_after_seconds=pending.interval_seconds
                    )
                return McpBrowserAuthorizationPending()
        return McpAuthorizationStatus(status=status)

    async def resolve(
        self,
        scope: OctomateSession | User,
        namespace: str | None = None,
        *,
        mcp_id: UUID7 | None = None,
    ) -> tuple[User, Mcp]:
        """Resolve one enabled installation owned by the caller, by namespace or UUID.

        Historical invocations supply only their saved UUID. A reused namespace
        therefore cannot substitute a different installation.
        """
        if (namespace is None) == (mcp_id is None):
            raise ValueError("Supply exactly one MCP namespace or installation ID")
        if isinstance(scope, User):
            user = scope
        else:
            user = (
                scope.user_profile.user.value
                if scope.user_profile is not None
                and scope.user_profile.user_id is not None
                else None
            )
        if user is None:
            raise McpUnavailable
        async with async_session() as session:
            instance = await session.one_or_none(
                Mcp,
                expressions=[
                    Mcp["user_id"] == user.id,
                    Mcp["id"] == mcp_id
                    if mcp_id is not None
                    else Mcp["namespace"] == namespace,
                    Mcp["enabled"].is_(True),
                ],
            )
        if instance is None:
            raise McpUnavailable
        return user, instance

    async def catalog(
        self,
        scope: OctomateSession | User,
        namespace: str | None = None,
        *,
        mcp_id: UUID7 | None = None,
        refresh: bool = False,
    ) -> McpToolCatalog:
        """Read cached discovery, loading it when absent or explicitly refreshed.

        Ownership, enabled state, and authorization are checked even on cache hits.
        The bounded catalog follows its pooled connection's lifetime.
        """
        user, instance = await self.resolve(scope, namespace, mcp_id=mcp_id)
        async with (
            self.acquire(user, mcp_id=instance.id) as client,
            self.lock(instance.id),
        ):
            key = McpClientKey(user.id, instance.id)
            catalog = self.tool_catalogs.get(key)
            if catalog is not None and not refresh:
                return catalog
            instructions = "\n".join(
                part for part in (instance.instructions, client.instructions) if part
            )
            catalog = McpToolCatalog(
                instructions=instructions, tools=await client.list_tools()
            )
            self.tool_catalogs[key] = catalog
            return catalog

    async def call_tool(
        self,
        scope: OctomateSession,
        namespace: str,
        name: str,
        arguments: dict[str, JsonValue],
    ) -> CallToolResult:
        """Execute one upstream call and retain its declared app before publishing.

        This boundary sees each call independently, including calls a runtime
        nests inside a code wrapper. Sessions without thread attribution return
        the upstream result without producing an artifact or ledger reference.
        App declarations reuse the connection's latest discovery, loading once
        when absent. Explicit discovery refreshes them; client eviction clears them.
        """
        user, instance = await self.resolve(scope, namespace)
        address = scope.conversation_address
        channel = scope.channels.get(address.channel_tentacle_id) if address else None
        async with self.acquire(user, mcp_id=instance.id) as client:
            if (
                scope.native
                or scope.thread_id is None
                or scope.threads is None
                or channel is None
                or address is None
            ):
                return await client.call_tool_mcp(name, arguments)
            catalog = await self.catalog(user, mcp_id=instance.id)
            tool = catalog.tools_by_name.get(name)
            ui = McpAppToolMeta.model_validate(tool.meta or {}) if tool else None
            result = await client.call_tool_mcp(name, arguments)
        if tool is None or ui is None or ui.resource_uri is None:
            return result
        message = await scope.threads.record_artifact(
            scope.thread_id,
            McpAppArtifact(
                owner_id=user.id,
                title=tool.title or tool.name,
                source=InstalledMcpAppSource(
                    installation_id=instance.id,
                    namespace=instance.namespace,
                ),
                invocation=McpAppInvocation(
                    provider_name=instance.name,
                    tool=tool,
                    resource_uri=ui.resource_uri,
                    arguments=arguments,
                    result=result,
                ),
            ),
            sender=channel.self_profile,
            agent_tentacle_id=scope.current_agent_id,
        )
        await scope.publish(
            address,
            ArtifactAvailableEvent(
                message_id=message.id, artifact_id=message.artifact_id
            ),
        )
        return result

    async def app_resource(
        self, user: User, installation_id: UUID7, invocation: McpAppInvocation
    ) -> McpAppResource:
        """Read only the saved UI URI from its still-authorized installation UUID.

        This performs one resource read, never discovery or tool execution. The
        response must contain one matching MCP Apps HTML resource, as text or
        base64-encoded UTF-8. UI metadata is validated before reaching a browser.
        """
        async with self.acquire(user, mcp_id=installation_id) as client:
            contents = await client.read_resource(invocation.resource_uri)
        if len(contents) != 1:
            raise ValueError("An MCP app must return exactly one UI resource")
        resource = contents[0]
        if (
            resource.uri != str(invocation.resource_uri)
            or resource.mime_type != UI_MIME_TYPE
        ):
            raise ValueError("MCP app resource URI or MIME type does not match")
        html = (
            resource.text
            if isinstance(resource, TextResourceContents)
            else b64decode(resource.blob, validate=True).decode("utf-8")
        )
        return McpAppResource(
            uri=invocation.resource_uri,
            html=html,
            ui=(resource.meta or {}).get("ui", {}),
        )

    @asynccontextmanager
    async def acquire(
        self,
        scope: OctomateSession | User,
        namespace: str | None = None,
        *,
        mcp_id: UUID7 | None = None,
    ) -> AsyncGenerator[Client]:
        user, instance = await self.resolve(scope, namespace, mcp_id=mcp_id)
        user_id = user.id
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
                    user,
                    current.tentacle_id or "mcp",
                    mcp_id=current.id,
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
                tentacle = self.tentacles.get(current.tentacle_id or "")
                if isinstance(current, BearerMcp):
                    if self.cipher is None:
                        raise ValueError("MCP credential encryption is not configured")
                    token = self.cipher.decrypt(
                        current.encrypted_token, context=f"mcp:{user_id}:{current.id}"
                    )
                    headers["Authorization"] = f"Bearer {token}"
                auth: McpBearerAuth | None = None
                if isinstance(current, OAuthMcp) and self.oauth is not None:
                    if isinstance(tentacle, LarkTentacle):
                        auth = LarkMcpBearerAuth(
                            manager=self.oauth,
                            user=user,
                            mcp_id=current.id,
                            url=current.url,
                            tentacle=tentacle,
                        )
                    else:
                        auth = McpBearerAuth(
                            manager=self.oauth,
                            user=user,
                            mcp_id=current.id,
                            connector_id=current.tentacle_id or "mcp",
                            url=current.url,
                        )
                client = Client(
                    transport=StreamableHttpTransport(
                        url=current.url,
                        headers=headers,
                        auth=auth,
                        httpx_client_factory=self.httpx_client_factory,
                    ),
                    mode="auto",
                    cache=False,
                    extensions=[
                        advertise(
                            UI_EXTENSION_ID,
                            {"mimeTypes": [UI_MIME_TYPE]},
                        )
                    ],
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
        self.tool_catalogs.pop(key, None)
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
