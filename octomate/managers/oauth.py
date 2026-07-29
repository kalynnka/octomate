from __future__ import annotations

import asyncio
import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timezone

from pydantic import SecretStr
from uuid_utils.compat import uuid7

from octomate.database import async_session
from octomate.managers.user import UserManager
from octomate.schemas.oauth import (
    AuthorizationCodeOAuthFlow,
    AuthorizationLink,
    DeviceAuthorization,
    DeviceOAuthFlow,
    DeviceOperationPayload,
    OAuthCipher,
    OAuthCallbackTransport,
    OAuthFlowContext,
    OAuthGrant,
    OAuthPending,
    OAuthStartResult,
    OAuthConnection,
    OAuthOperation,
    OAuthTokenPayload,
)
from octomate.schemas.user import UserProfile


class NoPendingAuthorization(ValueError):
    """Nothing of this connector's is waiting on this user to authorize it.

    A `ValueError` still, so the channel handlers reporting a failed connection
    keep catching it unchanged; the narrower type is what lets a tool tell the
    model to start an authorization rather than reporting the turn as broken.
    """


@dataclass(frozen=True, kw_only=True)
class OAuthConnector:
    """One registered integration's injected OAuth composition.

    It selects the upstream flow and, for authorization code, the deployment callback
    transport. It carries no user identity; ``OAuthManager`` supplies the current
    channel owner when an authorization starts.
    """

    id: str
    flow: DeviceOAuthFlow | AuthorizationCodeOAuthFlow
    callback_transport: OAuthCallbackTransport | None = None

    def __post_init__(self) -> None:
        if not self.id:
            raise ValueError("OAuth connector id cannot be empty")
        if isinstance(self.flow, DeviceOAuthFlow):
            if self.callback_transport is not None:
                raise ValueError("device OAuth does not use a callback transport")
            return
        if self.callback_transport is None:
            raise ValueError("authorization-code OAuth requires a callback transport")


class OAuthManager:
    """Registered OAuth connectors bound to the current channel user."""

    def __init__(
        self,
        *,
        users: UserManager,
        encryption_key: SecretStr | None = None,
        connectors: Iterable[OAuthConnector] = (),
    ) -> None:
        self.users = users
        self.cipher = (
            OAuthCipher(encryption_key) if encryption_key is not None else None
        )
        self.connectors: dict[str, OAuthConnector] = {}
        self.completion_lock = asyncio.Lock()
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

    async def start(
        self,
        profile: UserProfile,
        connector_id: str,
    ) -> OAuthStartResult:
        connector = self.connector(connector_id)
        user = await self.users.owner(profile)
        if user is None:
            raise ValueError("OAuth connections require a registered user")

        context = OAuthFlowContext(
            operation_id=uuid7(),
            connector_id=connector.id,
            user=user,
            profile=profile,
        )
        if isinstance(connector.flow, DeviceOAuthFlow):
            cipher = self.cipher
            if cipher is None:
                raise ValueError("OAuth persistence requires an encryption key")
            resumed = await self.live_device_authorization(
                user_id=user.id,
                profile_id=profile.id,
                connector_id=connector.id,
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
                profile_id=profile.id,
                connector_id=connector.id,
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
        callback_uri = await transport.callback_uri(connector.id)
        authorization = await flow.start(context, callback_uri)
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
        profile_id: uuid.UUID,
        connector_id: str,
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
                    OAuthOperation["consumed_at"].is_(None),
                    OAuthOperation["expires_at"] > datetime.now(timezone.utc),
                ],
            )
        if operation is None:
            return None
        expires_at = operation.expires_at
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=timezone.utc)
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
        profile: UserProfile,
        connector_id: str,
    ) -> OAuthGrant | OAuthPending:
        """Poll the newest pending device operation owned by ``profile``'s user."""
        user = await self.users.owner(profile)
        if user is None:
            raise ValueError("OAuth connections require a registered user")
        async with async_session() as session:
            operations = await session.list(
                OAuthOperation,
                limit=1,
                order_bys=[OAuthOperation["id"].desc()],
                expressions=[
                    OAuthOperation["user_id"] == user.id,
                    OAuthOperation["profile_id"] == profile.id,
                    OAuthOperation["connector_id"] == connector_id,
                    OAuthOperation["consumed_at"].is_(None),
                ],
            )
        if not operations:
            raise NoPendingAuthorization(f"no pending {connector_id} authorization")
        return await self.complete(profile, operations[0].id)

    async def complete(
        self,
        profile: UserProfile,
        operation_id: uuid.UUID,
    ) -> OAuthGrant | OAuthPending:
        """Complete one device operation without accepting an owner from the caller."""
        cipher = self.cipher
        if cipher is None:
            raise ValueError("OAuth persistence requires an encryption key")
        user = await self.users.owner(profile)
        if user is None:
            raise ValueError("OAuth connections require a registered user")

        async with self.completion_lock, async_session() as session:
            operation = await session.get(OAuthOperation, operation_id)
            if (
                operation is None
                or operation.user_id != user.id
                or operation.profile_id != profile.id
            ):
                raise ValueError("unknown OAuth operation")
            if operation.consumed_at is not None:
                raise ValueError("OAuth operation has already been consumed")
            expires_at = operation.expires_at
            if expires_at.tzinfo is None:
                expires_at = expires_at.replace(tzinfo=timezone.utc)
            if expires_at <= datetime.now(timezone.utc):
                operation.consumed_at = datetime.now(timezone.utc)
                await session.commit()
                raise ValueError("OAuth operation has expired")

            connector = self.connector(operation.connector_id)
            flow = connector.flow
            if not isinstance(flow, DeviceOAuthFlow):
                raise ValueError("OAuth operation is not a device authorization")
            context = OAuthFlowContext(
                operation_id=operation.id,
                connector_id=connector.id,
                user=user,
                profile=profile,
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

            connection = await session.one_or_none(
                OAuthConnection,
                expressions=[
                    OAuthConnection["user_id"] == user.id,
                    OAuthConnection["connector_id"] == connector.id,
                ],
            )
            if connection is None:
                connection = OAuthConnection(
                    user_id=user.id,
                    connector_id=connector.id,
                    encrypted_tokens=b"",
                    subject=result.subject,
                    account_label=result.account_label,
                )
                session.add(connection)
            payload = OAuthTokenPayload(
                access_token=result.access_token,
                refresh_token=result.refresh_token,
                token_type=result.token_type,
            )
            connection.status = "active"
            connection.encrypted_tokens = cipher.encrypt(
                payload.model_dump_json(),
                context=f"connection:{connection.id}",
            )
            connection.subject = result.subject
            connection.account_label = result.account_label
            connection.scopes = result.scopes
            connection.expires_at = result.expires_at
            connection.updated_at = datetime.now(timezone.utc)
            operation.consumed_at = datetime.now(timezone.utc)
            await session.commit()
        return result

    async def access_token(
        self,
        profile: UserProfile,
        connector_id: str,
    ) -> SecretStr | None:
        """Return the active connector token for the profile's registered owner."""
        cipher = self.cipher
        if cipher is None:
            return None
        user = await self.users.owner(profile)
        if user is None:
            return None
        async with async_session() as session:
            connection = await session.one_or_none(
                OAuthConnection,
                expressions=[
                    OAuthConnection["user_id"] == user.id,
                    OAuthConnection["connector_id"] == connector_id,
                    OAuthConnection["status"] == "active",
                ],
            )
        if connection is None:
            return None
        if connection.expires_at is not None:
            expires_at = connection.expires_at
            if expires_at.tzinfo is None:
                expires_at = expires_at.replace(tzinfo=timezone.utc)
            if expires_at <= datetime.now(timezone.utc):
                return None
        payload = OAuthTokenPayload.model_validate_json(
            cipher.decrypt(
                connection.encrypted_tokens,
                context=f"connection:{connection.id}",
            )
        )
        return payload.access_token
