from __future__ import annotations

import secrets
import uuid
from collections.abc import Iterable
from datetime import UTC, datetime, timedelta
from typing import NamedTuple, Self

from arcanus.materia.sqlalchemy import AsyncSession
from mcp.client.auth.utils import validate_authorization_response_iss
from mcp.shared._httpx_utils import McpHttpClientFactory
from pydantic import (
    AnyHttpUrl,
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    ValidationError,
    model_validator,
)
from uuid_utils.compat import uuid7

from octomate.config.oauth import OAuthConfig
from octomate.database import async_session
from octomate.managers.base import Locks, Manager
from octomate.managers.user import UserManager
from octomate.mcp.transport import mcp_http_client
from octomate.oauth.mcp import HTTPS_URL, McpOAuthFlow, OAuthRefreshRejected
from octomate.schemas.mcp import OAuthMcp
from octomate.schemas.oauth import (
    AuthorizationCodeOAuthFlow,
    AuthorizationCodeOperationPayload,
    AuthorizationLink,
    DeviceAuthorization,
    DeviceOAuthFlow,
    DeviceOperationPayload,
    DirectHttpOAuthCallbackTransport,
    McpOAuthState,
    OAuthCallbackTransport,
    OAuthCipher,
    OAuthConnection,
    OAuthFlowContext,
    OAuthGrant,
    OAuthOperation,
    OAuthPending,
    OAuthStartResult,
    OAuthTokenPayload,
)
from octomate.schemas.user import User, UserProfile
from octomate.types.oauth import HttpsUrl, OAuthConnectionStatus


class NoPendingAuthorization(ValueError):
    """Nothing of this connector's is waiting on this user to authorize it.

    A `ValueError` still, so the channel handlers reporting a failed connection
    keep catching it unchanged; the narrower type is what lets a tool tell the
    model to start an authorization rather than reporting the turn as broken.
    """


class UnusableOAuthOperation(ValueError):
    """The authorization a browser came back to is gone, spent, or not its own.

    One type for every way the round trip can fail to find its operation, because
    the browser is told the same thing by all of them — this link is finished, ask
    again — and only the log distinguishes them. A `ValueError` for the same reason
    `NoPendingAuthorization` is one.
    """


class OAuthConnector(BaseModel):
    """One registered integration's injected OAuth composition.

    It selects the upstream flow and, for authorization code, the deployment callback
    transport. It carries no user identity; ``OAuthManager`` supplies the current
    channel owner when an authorization starts.
    """

    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True)

    id: str = Field(min_length=1)
    flow: DeviceOAuthFlow | AuthorizationCodeOAuthFlow
    callback_transport: OAuthCallbackTransport | None = None
    mcp_url: HttpsUrl | None = None

    @model_validator(mode="after")
    def callback_matches_flow(self) -> Self:
        if isinstance(self.flow, DeviceOAuthFlow):
            if self.callback_transport is not None:
                raise ValueError("device OAuth does not use a callback transport")
            return self
        if self.callback_transport is None:
            raise ValueError("authorization-code OAuth requires a callback transport")
        return self


class OAuthLockKey(NamedTuple):
    user_id: uuid.UUID
    mcp_id: uuid.UUID | None
    connector_id: str


class OAuthManager(Manager, Locks[OAuthLockKey]):
    """Registered OAuth connectors bound to the current channel user."""

    def __init__(
        self,
        *,
        users: UserManager,
        encryption_key: SecretStr | None = None,
        connectors: Iterable[OAuthConnector] = (),
        callback_base_uri: AnyHttpUrl | None = None,
        client_metadata_url: HttpsUrl | None = None,
        token_refresh_leeway: timedelta | None = None,
        httpx_client_factory: McpHttpClientFactory = mcp_http_client,
    ) -> None:
        self.users = users
        self.cipher = (
            OAuthCipher(encryption_key) if encryption_key is not None else None
        )
        self.connectors: dict[str, OAuthConnector] = {}
        self.callback_base_uri = callback_base_uri
        self.client_metadata_url = client_metadata_url
        self.token_refresh_leeway = (
            token_refresh_leeway
            if token_refresh_leeway is not None
            else OAuthConfig().token_refresh_leeway
        )
        self.httpx_client_factory = httpx_client_factory
        for connector in connectors:
            self.register(connector)

    def register(self, connector: OAuthConnector) -> OAuthConnector:
        if connector.id in self.connectors:
            raise ValueError(f"OAuth connector {connector.id!r} is already registered")
        self.connectors[connector.id] = connector
        return connector

    def connector(self, connector_id: str) -> OAuthConnector:
        connector = self.connectors.get(connector_id)
        if connector is None:
            raise ValueError(f"unknown OAuth connector {connector_id!r}")
        return connector

    async def resolve_connector(
        self,
        connector_id: str,
        *,
        user_id: uuid.UUID,
        mcp_id: uuid.UUID | None,
        state: McpOAuthState | None = None,
    ) -> OAuthConnector:
        if mcp_id is None:
            return self.connector(connector_id)
        async with async_session() as session:
            mcp = await session.get(OAuthMcp, mcp_id)
        if mcp is None or mcp.user_id != user_id or not mcp.enabled:
            raise UnusableOAuthOperation("MCP authorization is unavailable")
        if connector_id != (mcp.tentacle_id or "mcp"):
            raise UnusableOAuthOperation("MCP authorization names another connector")
        url = HTTPS_URL.validate_python(mcp.url)
        if mcp.tentacle_id is not None:
            connector = self.connector(mcp.tentacle_id)
            if connector.mcp_url is None or str(connector.mcp_url) != str(url):
                raise UnusableOAuthOperation(
                    "MCP URL does not match the configured tentacle"
                )
            return connector
        if self.callback_base_uri is None:
            raise ValueError(
                "Configure oauth.callback_base_uri before connecting dynamic MCPs"
            )
        return OAuthConnector(
            id="mcp",
            flow=McpOAuthFlow(
                url=url,
                httpx_client_factory=self.httpx_client_factory,
                client_metadata_url=self.client_metadata_url,
                state=state,
            ),
            callback_transport=DirectHttpOAuthCallbackTransport(self.callback_base_uri),
        )

    async def start(
        self,
        user: User,
        connector_id: str,
        *,
        mcp_id: uuid.UUID | None = None,
        profile: UserProfile | None = None,
    ) -> OAuthStartResult:
        if profile is not None and profile.user_id != user.id:
            raise ValueError("OAuth profile does not belong to this user")

        connector = await self.resolve_connector(
            connector_id, user_id=user.id, mcp_id=mcp_id
        )
        context = OAuthFlowContext(
            operation_id=uuid7(),
            connector_id=connector.id,
            mcp_id=mcp_id,
            user=user,
            profile=profile,
        )
        if isinstance(connector.flow, DeviceOAuthFlow):
            cipher = self.cipher
            if cipher is None:
                raise ValueError("OAuth persistence requires an encryption key")
            resumed = await self.live_device_authorization(
                user_id=user.id,
                profile_id=profile.id if profile is not None else None,
                connector_id=connector.id,
                mcp_id=mcp_id,
            )
            if resumed is not None:
                return resumed
            authorization = await connector.flow.start(context)
            payload = DeviceOperationPayload(
                device_code=authorization.device_code,
                user_code=authorization.user_code,
                verification_uri=authorization.verification_uri,
                verification_uri_complete=authorization.verification_uri_complete,
            )
            operation = OAuthOperation(
                id=context.operation_id,
                user_id=user.id,
                profile_id=profile.id if profile is not None else None,
                connector_id=connector.id,
                mcp_id=mcp_id,
                encrypted_data=cipher.encrypt(
                    payload.model_dump_json(),
                    context=f"operation:{context.operation_id}",
                ),
                expires_at=authorization.expires_at,
                interval_seconds=authorization.interval_seconds,
            )
            async with async_session() as session:
                session.add(operation)
                await session.commit()
            return DeviceAuthorization(
                operation_id=context.operation_id,
                verification_uri=authorization.verification_uri,
                verification_uri_complete=authorization.verification_uri_complete,
                user_code=authorization.user_code,
                expires_at=authorization.expires_at,
                interval_seconds=authorization.interval_seconds,
            )

        flow = connector.flow
        if not isinstance(flow, AuthorizationCodeOAuthFlow):
            raise TypeError(f"unsupported OAuth flow {type(flow).__name__}")
        transport = connector.callback_transport
        if transport is None:
            raise ValueError("authorization-code OAuth requires a callback transport")
        cipher = self.cipher
        if cipher is None:
            raise ValueError("OAuth persistence requires an encryption key")

        callback_uri = await transport.callback_uri(connector.id)
        # The state names its own operation because the callback cannot: a provider
        # redirects to one URI per connector, with no room to say which authorization
        # came back. The random half is what makes it unforgeable, and it is why the
        # operation id alone will not do — that id travels in the public start link,
        # where anyone the link reaches can read it.
        state = SecretStr(f"{context.operation_id}.{secrets.token_urlsafe(32)}")
        try:
            authorization = await flow.start(context, callback_uri, state)
        except ValidationError:
            raise ValueError("OAuth authorization returned invalid metadata") from None
        payload = AuthorizationCodeOperationPayload(
            state=state,
            code_verifier=authorization.code_verifier,
            callback_uri=callback_uri,
            authorization_uri=authorization.authorization_uri,
            mcp_oauth=authorization.mcp_oauth,
        )
        operation = OAuthOperation(
            id=context.operation_id,
            user_id=user.id,
            profile_id=profile.id if profile is not None else None,
            connector_id=connector.id,
            mcp_id=mcp_id,
            encrypted_data=cipher.encrypt(
                payload.model_dump_json(),
                context=f"operation:{context.operation_id}",
            ),
            expires_at=authorization.expires_at,
        )
        async with async_session() as session:
            session.add(operation)
            await session.commit()
        # Staged before the link exists, because the direct-HTTP start route serves
        # the link by reading exactly this row back.
        public_uri = await transport.prepare_authorization(
            context,
            authorization.authorization_uri,
        )
        return AuthorizationLink(
            operation_id=context.operation_id,
            authorization_uri=public_uri,
            expires_at=authorization.expires_at,
        )

    async def live_device_authorization(
        self,
        *,
        user_id: uuid.UUID,
        profile_id: uuid.UUID | None,
        connector_id: str,
        mcp_id: uuid.UUID | None = None,
    ) -> DeviceAuthorization | None:
        """This user's device authorization that is still worth returning to.

        Asking to connect again while one is open would strand the code the user is
        already looking at, so the open one is handed back untouched; only once it
        has expired is a fresh flow the right answer.
        """
        cipher = self.cipher
        if cipher is None:
            raise ValueError("OAuth persistence requires an encryption key")
        async with async_session() as session:
            operation = await session.first(
                OAuthOperation,
                order_bys=[OAuthOperation["id"].desc()],
                expressions=[
                    OAuthOperation["user_id"] == user_id,
                    OAuthOperation["profile_id"] == profile_id,
                    OAuthOperation["connector_id"] == connector_id,
                    OAuthOperation["mcp_id"] == mcp_id,
                    OAuthOperation["consumed_at"].is_(None),
                    OAuthOperation["expires_at"] > datetime.now(UTC),
                ],
            )
        if operation is None:
            return None
        if operation.interval_seconds is None:
            raise ValueError(
                f"device operation {operation.id} has no polling interval; it was "
                "written by an authorization-code flow"
            )
        expires_at = operation.expires_at
        payload = DeviceOperationPayload.model_validate_json(
            cipher.decrypt(
                operation.encrypted_data,
                context=f"operation:{operation.id}",
            )
        )
        return DeviceAuthorization(
            operation_id=operation.id,
            verification_uri=payload.verification_uri,
            verification_uri_complete=payload.verification_uri_complete,
            user_code=payload.user_code,
            expires_at=expires_at,
            interval_seconds=operation.interval_seconds,
        )

    async def complete_latest(
        self,
        user: User,
        connector_id: str,
        *,
        mcp_id: uuid.UUID | None = None,
        profile: UserProfile | None = None,
    ) -> OAuthGrant | OAuthPending:
        """Poll this user's newest device operation from the originating context."""
        if profile is not None and profile.user_id != user.id:
            raise ValueError("OAuth profile does not belong to this user")
        async with async_session() as session:
            operations = await session.list(
                OAuthOperation,
                limit=1,
                order_bys=[OAuthOperation["id"].desc()],
                expressions=[
                    OAuthOperation["user_id"] == user.id,
                    OAuthOperation["profile_id"]
                    == (profile.id if profile is not None else None),
                    OAuthOperation["connector_id"] == connector_id,
                    OAuthOperation["mcp_id"] == mcp_id,
                    OAuthOperation["consumed_at"].is_(None),
                ],
            )
        if not operations:
            raise NoPendingAuthorization(f"no pending {connector_id} authorization")
        return await self.complete(user, operations[0].id, profile=profile)

    async def complete(
        self,
        user: User,
        operation_id: uuid.UUID,
        *,
        profile: UserProfile | None = None,
    ) -> OAuthGrant | OAuthPending:
        """Complete a device operation owned by this user and originating profile."""
        cipher = self.cipher
        if cipher is None:
            raise ValueError("OAuth persistence requires an encryption key")
        if profile is not None and profile.user_id != user.id:
            raise ValueError("OAuth profile does not belong to this user")

        async with async_session() as session:
            operation = await session.get(OAuthOperation, operation_id)
            if operation is None:
                raise ValueError("unknown OAuth operation")
            key = OAuthLockKey(
                user_id=user.id,
                mcp_id=operation.mcp_id,
                connector_id=operation.connector_id,
            )

        async with self.lock(key), async_session() as session:
            operation = await session.get(OAuthOperation, operation_id)
            if (
                operation is None
                or operation.user_id != user.id
                or (profile is not None and operation.profile_id != profile.id)
            ):
                raise ValueError("unknown OAuth operation")
            if operation.consumed_at is not None:
                raise ValueError("OAuth operation has already been consumed")
            expires_at = operation.expires_at
            if expires_at <= datetime.now(UTC):
                operation.consumed_at = datetime.now(UTC)
                await session.commit()
                raise ValueError("OAuth operation has expired")

            connector = await self.resolve_connector(
                operation.connector_id, user_id=user.id, mcp_id=operation.mcp_id
            )
            flow = connector.flow
            if not isinstance(flow, DeviceOAuthFlow):
                raise ValueError("OAuth operation is not a device authorization")
            context = OAuthFlowContext(
                operation_id=operation.id,
                connector_id=connector.id,
                user=user,
                profile=profile,
                mcp_id=operation.mcp_id,
            )
            payload = DeviceOperationPayload.model_validate_json(
                cipher.decrypt(
                    operation.encrypted_data,
                    context=f"operation:{operation.id}",
                )
            )
            result = await flow.complete(context, payload.device_code)
            if isinstance(result, OAuthPending):
                return result

            # Reauthorization replaces this grant, including when it is invalid.
            existing = await session.one_or_none(
                OAuthConnection,
                expressions=[
                    OAuthConnection["user_id"] == user.id,
                    OAuthConnection["connector_id"] == connector.id,
                    OAuthConnection["mcp_id"] == operation.mcp_id,
                ],
            )
            session.add(
                self.granted_connection(
                    result,
                    user_id=user.id,
                    connector_id=connector.id,
                    existing=existing,
                    mcp_id=operation.mcp_id,
                )
            )
            operation.consumed_at = datetime.now(UTC)
            await session.commit()
        return result

    def granted_connection(
        self,
        grant: OAuthGrant,
        *,
        user_id: uuid.UUID,
        connector_id: str,
        existing: OAuthConnection | None,
        mcp_id: uuid.UUID | None = None,
    ) -> OAuthConnection:
        """This user's connection to a connector, carrying a grant, encrypted.

        The one place credentials become storable, shared by every way of obtaining
        them — a device poll, a browser callback, a refresh — so replacement is one
        behaviour rather than three that drift. The whole token envelope is rewritten
        together: a refresh that kept half of the old one would leave a rotated
        refresh token pointing at a grant that no longer exists.

        Touches no session. Persisting is the caller's, because only the caller knows
        what else has to land in the same transaction — consuming the operation that
        produced the grant has to commit with it or not at all.
        """
        cipher = self.cipher
        if cipher is None:
            raise ValueError("OAuth persistence requires an encryption key")
        connection = existing or OAuthConnection(
            user_id=user_id,
            connector_id=connector_id,
            encrypted_tokens=b"",
            mcp_id=mcp_id,
            subject=grant.subject,
            account_label=grant.account_label,
        )
        connection.status = "active"
        connection.encrypted_tokens = cipher.encrypt(
            OAuthTokenPayload(
                access_token=grant.access_token,
                refresh_token=grant.refresh_token,
                token_type=grant.token_type,
                mcp_oauth=grant.mcp_oauth,
            ).model_dump_json(),
            context=f"connection:{connection.id}",
        )
        connection.subject = grant.subject
        connection.account_label = grant.account_label
        connection.scopes = grant.scopes
        connection.expires_at = grant.expires_at
        connection.updated_at = datetime.now(UTC)
        return connection

    async def staged_authorization(
        self,
        connector_id: str,
        operation_id: uuid.UUID,
    ) -> AuthorizationCodeOperationPayload:
        """The provider request a UUID-only start link stands for.

        Reading it is what keeps the provider's URI — client id, scopes, state, PKCE
        challenge — out of the message the user was sent, which carries nothing but
        this operation's id.
        """
        async with async_session() as session:
            operation = self.usable_operation(
                await session.get(OAuthOperation, operation_id), connector_id
            )
            return self.authorization_payload(operation)

    async def complete_callback(
        self,
        connector_id: str,
        *,
        state: str,
        code: str,
        issuer: str | None = None,
    ) -> OAuthGrant:
        """Finish an authorization-code operation from the provider's callback.

        Nothing about this request proves who sent it — it is an ordinary browser GET
        arriving out of band — so everything binding it to a person was decided when
        the authorization started and is checked again here. The state matches an
        operation that has not been spent or expired; that operation names the profile
        that asked; and the profile still has to resolve to the same registered user,
        because a YAML declaration removed mid-flow must not finish as a connection.
        """
        async with async_session() as session:
            operation, _ = await self.operation_for_state(session, connector_id, state)
            key = OAuthLockKey(
                user_id=operation.user_id,
                mcp_id=operation.mcp_id,
                connector_id=operation.connector_id,
            )

        async with self.lock(key), async_session() as session:
            operation, payload = await self.operation_for_state(
                session, connector_id, state
            )
            profile = (
                await session.get(UserProfile, operation.profile_id)
                if operation.profile_id is not None
                else None
            )
            if operation.profile_id is None:
                user = await session.get(User, operation.user_id)
            else:
                user = await self.users.owner(profile) if profile is not None else None
            if user is None or user.id != operation.user_id:
                raise UnusableOAuthOperation(
                    "the profile that started this authorization is no longer linked "
                    "to the user it was started for"
                )
            connector = await self.resolve_connector(
                operation.connector_id,
                user_id=user.id,
                mcp_id=operation.mcp_id,
                state=payload.mcp_oauth,
            )
            if payload.mcp_oauth is not None:
                validate_authorization_response_iss(issuer, payload.mcp_oauth.metadata)
            flow = connector.flow
            if not isinstance(flow, AuthorizationCodeOAuthFlow):
                raise UnusableOAuthOperation(
                    "OAuth operation is not an authorization-code authorization"
                )
            # Consume before contacting the provider, including when the exchange fails.
            operation.consumed_at = datetime.now(UTC)
            await session.commit()
            grant = await flow.exchange(
                OAuthFlowContext(
                    operation_id=operation.id,
                    connector_id=connector.id,
                    user=user,
                    profile=profile,
                    mcp_id=operation.mcp_id,
                ),
                code=code,
                code_verifier=payload.code_verifier,
                callback_uri=payload.callback_uri,
            )
            # Reauthorization replaces this grant, including when it is invalid.
            existing = await session.one_or_none(
                OAuthConnection,
                expressions=[
                    OAuthConnection["user_id"] == user.id,
                    OAuthConnection["connector_id"] == connector.id,
                    OAuthConnection["mcp_id"] == operation.mcp_id,
                ],
            )
            session.add(
                self.granted_connection(
                    grant,
                    user_id=user.id,
                    connector_id=connector.id,
                    existing=existing,
                    mcp_id=operation.mcp_id,
                )
            )
            await session.commit()
        return grant

    async def abandon_callback(
        self, connector_id: str, *, state: str, issuer: str | None = None
    ) -> None:
        """Close an operation the provider says the user turned down.

        A denial is an answer, so the operation is spent rather than left to expire —
        otherwise the link the user declined stays live for its full lifetime.
        """
        async with async_session() as session:
            operation, _ = await self.operation_for_state(session, connector_id, state)
            key = OAuthLockKey(
                user_id=operation.user_id,
                mcp_id=operation.mcp_id,
                connector_id=operation.connector_id,
            )

        async with self.lock(key), async_session() as session:
            operation, payload = await self.operation_for_state(
                session, connector_id, state
            )
            if payload.mcp_oauth is not None:
                validate_authorization_response_iss(issuer, payload.mcp_oauth.metadata)
            operation.consumed_at = datetime.now(UTC)
            await session.commit()

    async def operation_for_state(
        self,
        session: AsyncSession,
        connector_id: str,
        state: str,
    ) -> tuple[OAuthOperation, AuthorizationCodeOperationPayload]:
        """The live operation a callback's state belongs to, and its sealed payload.

        The state's own prefix names the operation, since the row cannot be searched
        by a value stored encrypted; the comparison that follows is against the whole
        state, so naming an operation is not the same as being able to finish it.
        """
        operation_id, _, _ = state.partition(".")
        try:
            found = await session.get(OAuthOperation, uuid.UUID(operation_id))
        except ValueError:
            raise UnusableOAuthOperation("malformed OAuth state") from None
        operation = self.usable_operation(found, connector_id)
        payload = self.authorization_payload(operation)
        if not secrets.compare_digest(payload.state.get_secret_value(), state):
            raise UnusableOAuthOperation("OAuth state does not match its operation")
        return operation, payload

    def usable_operation(
        self,
        operation: OAuthOperation | None,
        connector_id: str,
    ) -> OAuthOperation:
        """Refuse an operation that is missing, another connector's, spent, or stale."""
        if operation is None or operation.connector_id != connector_id:
            raise UnusableOAuthOperation("unknown OAuth operation")
        if operation.consumed_at is not None:
            raise UnusableOAuthOperation("OAuth operation has already been consumed")
        expires_at = operation.expires_at
        if expires_at <= datetime.now(UTC):
            raise UnusableOAuthOperation("OAuth operation has expired")
        return operation

    def authorization_payload(
        self,
        operation: OAuthOperation,
    ) -> AuthorizationCodeOperationPayload:
        cipher = self.cipher
        if cipher is None:
            raise ValueError("OAuth persistence requires an encryption key")
        return AuthorizationCodeOperationPayload.model_validate_json(
            cipher.decrypt(
                operation.encrypted_data,
                context=f"operation:{operation.id}",
            )
        )

    async def invalidate(
        self,
        user: User,
        connector_id: str,
        *,
        mcp_id: uuid.UUID | None = None,
        expected_token: SecretStr | None = None,
    ) -> None:
        """Record that this user's credentials for a connector no longer work.

        Only the provider can say so — a revoked token answers 401 while nothing
        about the stored row changed — so something that spoke to it has to bring
        the news back. Marking it is what stops `access_token` handing the dead
        credential out again, which is what turns a capability that can only fail
        into an offer to authorize afresh.

        Nothing to mark is a normal outcome: a second 401 from the same dead
        session finds it already recorded.
        """
        async with (
            self.lock(
                OAuthLockKey(user_id=user.id, mcp_id=mcp_id, connector_id=connector_id)
            ),
            async_session() as session,
        ):
            connection = await session.one_or_none(
                OAuthConnection,
                expressions=[
                    OAuthConnection["user_id"] == user.id,
                    OAuthConnection["connector_id"] == connector_id,
                    OAuthConnection["mcp_id"] == mcp_id,
                    OAuthConnection["status"] == "active",
                ],
            )
            if connection is None:
                return
            if expected_token is not None:
                if self.cipher is None:
                    raise ValueError("OAuth persistence requires an encryption key")
                payload = OAuthTokenPayload.model_validate_json(
                    self.cipher.decrypt(
                        connection.encrypted_tokens,
                        context=f"connection:{connection.id}",
                    )
                )
                if payload.access_token != expected_token:
                    return
            connection.status = "invalid"
            connection.updated_at = datetime.now(UTC)
            await session.commit()

    async def connection_status(
        self,
        user: User,
        connector_id: str,
        *,
        mcp_id: uuid.UUID | None = None,
    ) -> OAuthConnectionStatus | None:
        """Whether this user has a connection to a connector, and whether it works.

        `None` is never having connected; `"invalid"` is having connected and lost
        it. `access_token` collapses both to no token, which is right for using one
        and wrong for explaining its absence — a user who was connected a moment ago
        cannot see that they no longer are, and only this tells them apart.
        """
        async with async_session() as session:
            connection = await session.one_or_none(
                OAuthConnection,
                expressions=[
                    OAuthConnection["user_id"] == user.id,
                    OAuthConnection["connector_id"] == connector_id,
                    OAuthConnection["mcp_id"] == mcp_id,
                ],
            )
        return connection.status if connection is not None else None

    async def access_token(
        self,
        user: User,
        connector_id: str,
        *,
        mcp_id: uuid.UUID | None = None,
    ) -> SecretStr | None:
        """Return this user's active connector token."""
        cipher = self.cipher
        if cipher is None:
            return None
        async with async_session() as session:
            connection = await session.one_or_none(
                OAuthConnection,
                expressions=[
                    OAuthConnection["user_id"] == user.id,
                    OAuthConnection["connector_id"] == connector_id,
                    OAuthConnection["mcp_id"] == mcp_id,
                    OAuthConnection["status"] == "active",
                ],
            )
        if connection is None:
            return None
        payload = OAuthTokenPayload.model_validate_json(
            cipher.decrypt(
                connection.encrypted_tokens,
                context=f"connection:{connection.id}",
            )
        )
        if connection.expires_at is None or not self.expiring(connection.expires_at):
            return payload.access_token
        if payload.refresh_token is None:
            # Expiry is the one death the row can announce by itself; record it
            # so the next run offers a fresh authorization rather than re-reading
            # the same dead token.
            await self.invalidate(
                user,
                connector_id,
                mcp_id=mcp_id,
                expected_token=payload.access_token,
            )
            return None
        return await self.refresh(user, connector_id, mcp_id=mcp_id)

    async def refresh(
        self,
        user: User,
        connector_id: str,
        *,
        mcp_id: uuid.UUID | None = None,
    ) -> SecretStr | None:
        """Spend this user's refresh token for a credential worth starting a run on.

        Serialized, and re-read once the lock is held, because a rotating refresh
        token is spendable exactly once: two runs racing on the same near-expired
        connection would send the loser to the provider with a token the winner had
        already rotated, and the connection would die of being used correctly.

        A provider that refuses the refresh has ended the connection, so it retires
        the same way a 401 does. A provider that merely failed to answer raises,
        because nothing about the credential was learned.
        """
        cipher = self.cipher
        if cipher is None:
            return None
        async with (
            self.lock(
                OAuthLockKey(user_id=user.id, mcp_id=mcp_id, connector_id=connector_id)
            ),
            async_session() as session,
        ):
            connection = await session.one_or_none(
                OAuthConnection,
                expressions=[
                    OAuthConnection["user_id"] == user.id,
                    OAuthConnection["connector_id"] == connector_id,
                    OAuthConnection["mcp_id"] == mcp_id,
                    OAuthConnection["status"] == "active",
                ],
            )
            if connection is None:
                return None
            payload = OAuthTokenPayload.model_validate_json(
                cipher.decrypt(
                    connection.encrypted_tokens,
                    context=f"connection:{connection.id}",
                )
            )
            # Re-read, so a run that lost the race finds the winner's credential
            # instead of spending a refresh token that has already been rotated.
            if connection.expires_at is None or not self.expiring(
                connection.expires_at
            ):
                return payload.access_token
            if payload.refresh_token is None:
                return None
            connector = await self.resolve_connector(
                connector_id, user_id=user.id, mcp_id=mcp_id, state=payload.mcp_oauth
            )
            flow = connector.flow
            if not isinstance(flow, AuthorizationCodeOAuthFlow):
                raise ValueError(f"{connector_id!r} has no refreshable OAuth flow")
            try:
                grant = await flow.refresh(payload.refresh_token)
            except ValueError as error:
                if isinstance(error, ValidationError):
                    raise ValueError(
                        "OAuth refresh returned invalid credentials"
                    ) from None
                if isinstance(flow, McpOAuthFlow) and not isinstance(
                    error, OAuthRefreshRejected
                ):
                    raise
                connection.status = "invalid"
                connection.updated_at = datetime.now(UTC)
                await session.commit()
                return None
            # Already loaded and already this user's, so it is handed straight
            # back rather than looked up again.
            self.granted_connection(
                grant,
                user_id=user.id,
                connector_id=connector_id,
                existing=connection,
                mcp_id=mcp_id,
            )
            await session.commit()
        return grant.access_token

    def expiring(self, expires_at: datetime) -> bool:
        """Whether a credential has too little life left to start a run on."""
        return expires_at <= datetime.now(UTC) + self.token_refresh_leeway
