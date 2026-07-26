from __future__ import annotations

import base64
import binascii
import hashlib
import os
import secrets
import uuid
from datetime import datetime, timedelta, timezone

from arcanus.materia.sqlalchemy import AsyncSession
from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from pydantic import BaseModel, JsonValue, SecretStr, TypeAdapter
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm.exc import StaleDataError
from uuid_utils.compat import uuid7

from octomate.config.oauth import (
    McpOAuthConnectionConfig,
    OAuthConfig,
    OAuthConnectionConfig,
    OAuthSecuritySettings,
    ProviderOAuthConnectionConfig,
)
from octomate.database import async_session
from octomate.schemas.oauth import (
    McpOAuthCompletion,
    McpOAuthConnection,
    OAuthAuthorizationTicket,
    OAuthCallbackContext,
    OAuthCompletion,
    OAuthConnection,
    OAuthConnectionAdapter,
    OAuthConnectionSummary,
    OAuthPrivateData,
    OAuthStartContext,
    OAuthTokenDocument,
    OAuthTransaction,
    OAuthTransactionSecrets,
    ProviderOAuthCompletion,
    ProviderOAuthConnection,
)
from octomate.schemas.user import UserProfile

OAUTH_TRANSACTION_TTL = timedelta(minutes=10)
ENCRYPTION_ENVELOPE_VERSION = 1
TOKEN_DOCUMENT_ADAPTER = TypeAdapter(dict[str, JsonValue])


class EncryptionEnvelope(BaseModel):
    version: int
    key_id: str
    nonce: str
    ciphertext: str


class OAuthCipher:
    """Authenticated encryption for OAuth documents with decrypt-only old keys."""

    def __init__(self, settings: OAuthSecuritySettings) -> None:
        self.primary_key_id = settings.primary_key_id
        self.keys = {
            key_id: self.decode_key(secret)
            for key_id, secret in settings.encryption_keys.items()
        }

    @staticmethod
    def decode_key(secret: SecretStr) -> bytes:
        encoded = secret.get_secret_value()
        try:
            key = base64.b64decode(
                encoded + "=" * (-len(encoded) % 4),
                altchars=b"-_",
                validate=True,
            )
        except (binascii.Error, ValueError) as error:
            raise ValueError(
                "OAuth encryption keys must be base64url encoded"
            ) from error
        if len(key) != 32:
            raise ValueError("OAuth encryption keys must decode to 32 bytes")
        return key

    def encrypt(self, document: dict[str, JsonValue], *, context: str) -> bytes:
        nonce = os.urandom(12)
        ciphertext = AESGCM(self.keys[self.primary_key_id]).encrypt(
            nonce,
            TOKEN_DOCUMENT_ADAPTER.dump_json(document),
            context.encode(),
        )
        return (
            EncryptionEnvelope(
                version=ENCRYPTION_ENVELOPE_VERSION,
                key_id=self.primary_key_id,
                nonce=base64.urlsafe_b64encode(nonce).decode(),
                ciphertext=base64.urlsafe_b64encode(ciphertext).decode(),
            )
            .model_dump_json()
            .encode()
        )

    def decrypt(self, encrypted: bytes, *, context: str) -> dict[str, JsonValue]:
        envelope = EncryptionEnvelope.model_validate_json(encrypted)
        if envelope.version != ENCRYPTION_ENVELOPE_VERSION:
            raise ValueError(
                f"unsupported OAuth encryption envelope {envelope.version}"
            )
        key = self.keys.get(envelope.key_id)
        if key is None:
            raise ValueError(f"unknown OAuth encryption key {envelope.key_id!r}")
        try:
            plaintext = AESGCM(key).decrypt(
                base64.urlsafe_b64decode(envelope.nonce),
                base64.urlsafe_b64decode(envelope.ciphertext),
                context.encode(),
            )
        except (InvalidTag, ValueError) as error:
            raise ValueError("OAuth encrypted data failed authentication") from error
        return TOKEN_DOCUMENT_ADAPTER.validate_json(plaintext)


def secret_hash(secret: SecretStr) -> bytes:
    return hashlib.sha256(secret.get_secret_value().encode()).digest()


def code_challenge(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode()).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode()


def expired(expires_at: datetime, *, now: datetime) -> bool:
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    return expires_at <= now


def summarize(connection: OAuthConnection) -> OAuthConnectionSummary:
    variant = OAuthConnectionAdapter.validate_python(connection)
    return OAuthConnectionSummary(
        id=variant.id,
        kind=variant.kind,
        key=variant.key,
        status=variant.status,
        subject=variant.subject,
        account_label=variant.account_label,
        scopes=variant.scopes,
        expires_at=variant.expires_at,
        version=variant.version,
    )


class ConnectionManager:
    """Owner-bound lifecycle for encrypted provider and MCP connections."""

    def __init__(
        self,
        config: OAuthConfig,
        security: OAuthSecuritySettings,
    ) -> None:
        self.config = config
        self.cipher = OAuthCipher(security)

    def definition(self, key: str) -> OAuthConnectionConfig:
        definition = self.config.connections.get(key)
        if definition is None:
            raise ValueError(f"unknown OAuth connection {key!r}")
        return definition

    async def linked_profile(
        self,
        session: AsyncSession,
        profile: UserProfile,
    ) -> tuple[UserProfile, uuid.UUID]:
        stored = await session.get(UserProfile, profile.id)
        if stored is None or stored.user_id is None:
            raise ValueError("OAuth connections require a currently linked profile")
        return stored, stored.user_id

    async def begin(
        self,
        profile: UserProfile,
        key: str,
        *,
        replace: bool = False,
        data: OAuthPrivateData | None = None,
        ttl: timedelta = OAUTH_TRANSACTION_TTL,
    ) -> OAuthAuthorizationTicket:
        definition = self.definition(key)
        now = datetime.now(timezone.utc)
        ticket = SecretStr(secrets.token_urlsafe(32))
        state = secrets.token_urlsafe(32)
        verifier = secrets.token_urlsafe(64)

        async with async_session() as session:
            stored_profile, user_id = await self.linked_profile(session, profile)
            existing = await session.one_or_none(
                OAuthConnection,
                expressions=[
                    OAuthConnection["user_id"] == user_id,
                    OAuthConnection["key"] == key,
                ],
            )
            if existing is not None and not replace:
                raise ValueError(
                    f"OAuth connection {key!r} already exists; replacement is required"
                )

            transaction = OAuthTransaction(
                user_id=user_id,
                profile_id=stored_profile.id,
                kind=definition.kind,
                key=key,
                replace_existing=replace,
                ticket_hash=secret_hash(ticket),
                state_hash=secret_hash(SecretStr(state)),
                encrypted_data=b"",
                expires_at=now + ttl,
            )
            transaction.encrypted_data = self.cipher.encrypt(
                OAuthTransactionSecrets(
                    state=state,
                    code_verifier=verifier,
                    data=data or {},
                ).model_dump(),
                context=f"transaction:{transaction.id}",
            )
            session.add(transaction)
            await session.commit()

        return OAuthAuthorizationTicket(
            transaction_id=transaction.id,
            ticket=ticket,
            expires_at=transaction.expires_at,
        )

    async def redeem_ticket(self, ticket: SecretStr) -> OAuthStartContext:
        now = datetime.now(timezone.utc)
        async with async_session() as session:
            transaction = await session.one_or_none(
                OAuthTransaction,
                expressions=[OAuthTransaction["ticket_hash"] == secret_hash(ticket)],
            )
            if (
                transaction is None
                or transaction.started_at is not None
                or transaction.consumed_at is not None
                or expired(transaction.expires_at, now=now)
            ):
                raise ValueError("invalid or expired OAuth ticket")
            profile = await session.get(UserProfile, transaction.profile_id)
            if profile is None or profile.user_id != transaction.user_id:
                raise ValueError("OAuth transaction profile is no longer linked")

            secrets_document = self.cipher.decrypt(
                transaction.encrypted_data,
                context=f"transaction:{transaction.id}",
            )
            transaction_secrets = OAuthTransactionSecrets.model_validate(
                secrets_document
            )
            transaction.started_at = now
            try:
                await session.commit()
            except StaleDataError as error:
                raise ValueError("invalid or expired OAuth ticket") from error

        return OAuthStartContext(
            transaction_id=transaction.id,
            kind=transaction.kind,
            key=transaction.key,
            state=SecretStr(transaction_secrets.state),
            code_challenge=code_challenge(transaction_secrets.code_verifier),
            data=transaction_secrets.data,
        )

    async def claim_callback(self, state: SecretStr) -> OAuthCallbackContext:
        now = datetime.now(timezone.utc)
        async with async_session() as session:
            transaction = await session.one_or_none(
                OAuthTransaction,
                expressions=[OAuthTransaction["state_hash"] == secret_hash(state)],
            )
            if (
                transaction is None
                or transaction.started_at is None
                or transaction.callback_started_at is not None
                or transaction.consumed_at is not None
                or expired(transaction.expires_at, now=now)
            ):
                raise ValueError("invalid or expired OAuth state")
            profile = await session.get(UserProfile, transaction.profile_id)
            if profile is None or profile.user_id != transaction.user_id:
                raise ValueError("OAuth transaction profile is no longer linked")

            transaction_secrets = OAuthTransactionSecrets.model_validate(
                self.cipher.decrypt(
                    transaction.encrypted_data,
                    context=f"transaction:{transaction.id}",
                )
            )
            transaction.callback_started_at = now
            try:
                await session.commit()
            except StaleDataError as error:
                raise ValueError("invalid or expired OAuth state") from error

        return OAuthCallbackContext(
            transaction_id=transaction.id,
            kind=transaction.kind,
            key=transaction.key,
            code_verifier=SecretStr(transaction_secrets.code_verifier),
            data=transaction_secrets.data,
        )

    async def complete(
        self,
        callback: OAuthCallbackContext,
        completion: OAuthCompletion,
    ) -> OAuthConnectionSummary:
        if completion.kind != callback.kind:
            raise ValueError("OAuth completion kind does not match its transaction")
        definition = self.definition(callback.key)
        if definition.kind != callback.kind:
            raise ValueError(
                "OAuth connection configuration changed during authorization"
            )

        now = datetime.now(timezone.utc)
        async with async_session() as session:
            transaction = await session.get(OAuthTransaction, callback.transaction_id)
            if (
                transaction is None
                or transaction.callback_started_at is None
                or transaction.consumed_at is not None
                or expired(transaction.expires_at, now=now)
                or transaction.kind != callback.kind
                or transaction.key != callback.key
            ):
                raise ValueError("invalid or consumed OAuth transaction")
            profile = await session.get(UserProfile, transaction.profile_id)
            if profile is None or profile.user_id != transaction.user_id:
                raise ValueError("OAuth transaction profile is no longer linked")

            connection = await session.one_or_none(
                OAuthConnection,
                expressions=[
                    OAuthConnection["user_id"] == transaction.user_id,
                    OAuthConnection["key"] == transaction.key,
                ],
            )
            if connection is not None and not transaction.replace_existing:
                raise ValueError("OAuth connection replacement was not authorized")
            if connection is not None:
                existing_variant = OAuthConnectionAdapter.validate_python(connection)
                if existing_variant.kind != definition.kind:
                    raise ValueError(
                        "OAuth connection kind changed; disconnect it before reconnecting"
                    )

            connection_id = connection.id if connection is not None else uuid7()
            encrypted_tokens = self.cipher.encrypt(
                completion.tokens,
                context=f"connection:{connection_id}:tokens",
            )
            if connection is None:
                if isinstance(definition, ProviderOAuthConnectionConfig) and isinstance(
                    completion, ProviderOAuthCompletion
                ):
                    connection = ProviderOAuthConnection(
                        id=connection_id,
                        user_id=transaction.user_id,
                        key=transaction.key,
                        provider=definition.provider,
                        encrypted_tokens=encrypted_tokens,
                    )
                elif isinstance(definition, McpOAuthConnectionConfig) and isinstance(
                    completion, McpOAuthCompletion
                ):
                    connection = McpOAuthConnection(
                        id=connection_id,
                        user_id=transaction.user_id,
                        key=transaction.key,
                        resource_url=str(definition.resource_url),
                        authorization_server=completion.authorization_server,
                        encrypted_tokens=encrypted_tokens,
                    )
                else:
                    raise ValueError(
                        "OAuth completion does not match its configured connection"
                    )
                session.add(connection)
            else:
                connection.encrypted_tokens = encrypted_tokens

            connection.status = "active"
            connection.subject = completion.subject
            connection.account_label = completion.account_label
            connection.scopes = completion.scopes
            connection.expires_at = completion.expires_at
            connection.updated_at = now

            if (
                isinstance(connection, ProviderOAuthConnection)
                and isinstance(definition, ProviderOAuthConnectionConfig)
                and isinstance(completion, ProviderOAuthCompletion)
            ):
                connection.provider = definition.provider
            elif (
                isinstance(connection, McpOAuthConnection)
                and isinstance(definition, McpOAuthConnectionConfig)
                and isinstance(completion, McpOAuthCompletion)
            ):
                connection.resource_url = str(definition.resource_url)
                connection.authorization_server = completion.authorization_server
                connection.encrypted_client_information = (
                    self.cipher.encrypt(
                        completion.client_information,
                        context=f"connection:{connection.id}:client",
                    )
                    if completion.client_information is not None
                    else None
                )
            else:
                raise ValueError(
                    "OAuth completion does not match its configured connection"
                )

            transaction.consumed_at = now
            try:
                await session.commit()
            except (IntegrityError, StaleDataError) as error:
                raise ValueError(
                    "OAuth connection was completed concurrently"
                ) from error
            await session.refresh(connection)
            return summarize(connection)

    async def list(self, profile: UserProfile) -> list[OAuthConnectionSummary]:
        async with async_session() as session:
            _, user_id = await self.linked_profile(session, profile)
            connections = await session.list(
                OAuthConnection,
                expressions=[OAuthConnection["user_id"] == user_id],
                limit=None,
            )
            return [summarize(connection) for connection in connections]

    async def get_token(
        self,
        profile: UserProfile,
        key: str,
    ) -> OAuthTokenDocument:
        self.definition(key)
        async with async_session() as session:
            _, user_id = await self.linked_profile(session, profile)
            connection = await session.one_or_none(
                OAuthConnection,
                expressions=[
                    OAuthConnection["user_id"] == user_id,
                    OAuthConnection["key"] == key,
                ],
            )
            if connection is None or connection.status != "active":
                raise ValueError(f"OAuth connection {key!r} is not active")
            if connection.expires_at is not None and expired(
                connection.expires_at,
                now=datetime.now(timezone.utc),
            ):
                raise ValueError(f"OAuth connection {key!r} requires refresh")
            return self.cipher.decrypt(
                connection.encrypted_tokens,
                context=f"connection:{connection.id}:tokens",
            )

    async def replace_tokens(
        self,
        profile: UserProfile,
        key: str,
        *,
        expected_version: int,
        tokens: OAuthTokenDocument,
        expires_at: datetime | None,
    ) -> OAuthConnectionSummary:
        self.definition(key)
        try:
            async with async_session() as session:
                _, user_id = await self.linked_profile(session, profile)
                connection = await session.one_or_none(
                    OAuthConnection,
                    expressions=[
                        OAuthConnection["user_id"] == user_id,
                        OAuthConnection["key"] == key,
                    ],
                )
                if connection is None or connection.status != "active":
                    raise ValueError(f"OAuth connection {key!r} is not active")
                if connection.version != expected_version:
                    raise ValueError("OAuth connection was refreshed concurrently")
                connection.encrypted_tokens = self.cipher.encrypt(
                    tokens,
                    context=f"connection:{connection.id}:tokens",
                )
                connection.expires_at = expires_at
                connection.updated_at = datetime.now(timezone.utc)
                await session.commit()
                await session.refresh(connection)
                return summarize(connection)
        except StaleDataError as error:
            raise ValueError("OAuth connection was refreshed concurrently") from error

    async def mark_invalid(
        self,
        profile: UserProfile,
        key: str,
    ) -> OAuthConnectionSummary:
        self.definition(key)
        async with async_session() as session:
            _, user_id = await self.linked_profile(session, profile)
            connection = await session.one_or_none(
                OAuthConnection,
                expressions=[
                    OAuthConnection["user_id"] == user_id,
                    OAuthConnection["key"] == key,
                ],
            )
            if connection is None:
                raise ValueError(f"unknown OAuth connection {key!r}")
            connection.status = "invalid"
            connection.updated_at = datetime.now(timezone.utc)
            await session.commit()
            await session.refresh(connection)
            return summarize(connection)

    async def disconnect(
        self,
        profile: UserProfile,
        key: str,
    ) -> OAuthConnectionSummary:
        self.definition(key)
        async with async_session() as session:
            _, user_id = await self.linked_profile(session, profile)
            connection = await session.one_or_none(
                OAuthConnection,
                expressions=[
                    OAuthConnection["user_id"] == user_id,
                    OAuthConnection["key"] == key,
                ],
            )
            if connection is None:
                raise ValueError(f"unknown OAuth connection {key!r}")
            summary = summarize(connection)
            await session.delete(connection)
            await session.commit()
            return summary

    async def cleanup_transactions(self) -> int:
        now = datetime.now(timezone.utc)
        async with async_session() as session:
            transactions = await session.list(OAuthTransaction, limit=None)
            expired_transactions = [
                transaction
                for transaction in transactions
                if transaction.consumed_at is not None
                or expired(transaction.expires_at, now=now)
            ]
            for transaction in expired_transactions:
                await session.delete(transaction)
            await session.commit()
        return len(expired_transactions)
