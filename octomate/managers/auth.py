"""Local accounts, browser sessions, API keys, and inbound OAuth grants."""

from __future__ import annotations

import hashlib
import hmac
import secrets
from datetime import UTC, datetime

from anyio import CapacityLimiter, to_thread
from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError
from pydantic import UUID7, AwareDatetime, SecretStr
from sqlalchemy import or_, text
from sqlalchemy.orm.exc import StaleDataError
from uuid_utils.compat import uuid7

from octomate.config.auth import AuthConfig
from octomate.database import async_session
from octomate.managers.base import Locks, Manager
from octomate.schemas.auth import (
    ApiKey,
    RuntimeApiKey,
    SessionTokens,
    UserApiKey,
    UserInvitation,
    UserSession,
)
from octomate.schemas.authorization import (
    OAuthAuthorization,
    OAuthClient,
    OAuthGrant,
    OAuthPrincipal,
    OAuthToken,
)
from octomate.schemas.user import User
from octomate.types.auth import ApiKeyScope, NewPasswordAdapter


class InvalidCredentials(ValueError):
    """An authentication or credential exchange that is invalid or expired."""

    def __init__(self) -> None:
        super().__init__("Invalid or expired credentials")


class UsernameUnavailable(ValueError):
    """A registration whose username is already taken."""

    def __init__(self) -> None:
        super().__init__("This username is already taken")


class InvalidOAuthScopes(ValueError):
    """Requested scopes are empty or exceed the existing authorization."""

    def __init__(self) -> None:
        super().__init__("Scopes must be a nonempty subset of the authorization")


class AuthManager(Manager, Locks[str]):
    """Invite-only registration, local passwords, and user credentials."""

    def __init__(self, config: AuthConfig) -> None:
        self.config: AuthConfig = config
        self.password_hasher: PasswordHasher = PasswordHasher()
        self.password_limiter: CapacityLimiter = CapacityLimiter(2)
        # Unknown and unenrolled users still pay the password verification cost.
        self.dummy_password_hash: str = self.password_hasher.hash(
            secrets.token_urlsafe(32)
        )

    async def hash_password(self, password: SecretStr) -> SecretStr:
        return SecretStr(
            await to_thread.run_sync(
                self.password_hasher.hash,
                password.get_secret_value(),
                limiter=self.password_limiter,
            )
        )

    @staticmethod
    def hash_token(token: SecretStr, salt: SecretStr) -> SecretStr:
        return SecretStr(
            hashlib.sha256(
                (token.get_secret_value() + salt.get_secret_value()).encode()
            ).hexdigest()
        )

    async def invite(self) -> SecretStr:
        """Issue an anonymous code that admits one new account."""
        token = SecretStr(secrets.token_urlsafe(32))
        async with async_session() as session:
            session.add(
                UserInvitation(
                    token_hash=SecretStr(
                        hashlib.sha256(token.get_secret_value().encode()).hexdigest()
                    ),
                    expires_at=datetime.now(UTC) + self.config.invitation_lifetime,
                )
            )
            await session.commit()
            return token

    async def register(
        self, username: str, password: SecretStr, invitation: SecretStr, *, name: str
    ) -> User:
        if not username or username != username.strip() or len(username) > 100:
            raise ValueError(
                "Username must contain 1-100 characters without surrounding spaces"
            )
        password = NewPasswordAdapter.validate_python(password)
        if not 1 <= len(name) <= 100:
            raise ValueError("Display name must contain 1-100 characters")
        async with self.lock(username), async_session() as session:
            token_hash = SecretStr(
                hashlib.sha256(invitation.get_secret_value().encode()).hexdigest()
            )
            current = await session.one_or_none(
                UserInvitation,
                expressions=[
                    UserInvitation["token_hash"] == token_hash,
                    UserInvitation["consumed_at"].is_(None),
                    UserInvitation["expires_at"] > datetime.now(UTC),
                ],
            )
            if current is None:
                raise InvalidCredentials
            if (
                await session.one_or_none(
                    User, expressions=[User["username"] == username]
                )
                is not None
            ):
                raise UsernameUnavailable
            user = User(
                username=username,
                name=name,
                password_hash=await self.hash_password(password),
            )
            session.add(user)
            now = datetime.now(UTC)
            if current.expires_at <= now:
                raise InvalidCredentials
            current.consumed_at = now
            try:
                await session.flush()
            except StaleDataError as error:
                raise InvalidCredentials from error
            await session.commit()
            return user

    async def login(self, username: str, password: SecretStr) -> SessionTokens:
        async with async_session() as session:
            user = await session.one_or_none(
                User, expressions=[User["username"] == username]
            )
            password_hash = (
                user.password_hash.get_secret_value()
                if user is not None and user.password_hash is not None
                else self.dummy_password_hash
            )
            try:
                await to_thread.run_sync(
                    self.password_hasher.verify,
                    password_hash,
                    password.get_secret_value(),
                    limiter=self.password_limiter,
                )
            except VerifyMismatchError as error:
                raise InvalidCredentials from error
            if user is None or user.password_hash is None:
                raise InvalidCredentials
            if self.password_hasher.check_needs_rehash(password_hash):
                user.password_hash = await self.hash_password(password)

            now = datetime.now(UTC)
            tokens = SessionTokens(
                session_id=uuid7(),
                user_id=user.id,
                access_token=SecretStr(secrets.token_urlsafe(32)),
                refresh_token=SecretStr(secrets.token_urlsafe(32)),
                access_expires_at=now + self.config.access_token_lifetime,
                refresh_expires_at=now + self.config.session_lifetime,
            )
            session.add(
                UserSession(
                    id=tokens.session_id,
                    user_id=user.id,
                    access_token_hash=self.hash_token(
                        tokens.access_token, self.config.access_token_salt
                    ),
                    refresh_token_hash=self.hash_token(
                        tokens.refresh_token, self.config.refresh_token_salt
                    ),
                    access_expires_at=tokens.access_expires_at,
                    refresh_expires_at=tokens.refresh_expires_at,
                )
            )
            await session.commit()
            return tokens

    async def authenticate_session(self, token: SecretStr) -> UserSession | None:
        now = datetime.now(UTC)
        async with async_session() as session:
            return await session.one_or_none(
                UserSession,
                expressions=[
                    UserSession["access_token_hash"]
                    == self.hash_token(token, self.config.access_token_salt),
                    UserSession["revoked_at"].is_(None),
                    UserSession["access_expires_at"] > now,
                    UserSession["refresh_expires_at"] > now,
                ],
            )

    async def set_password(
        self,
        username: str,
        password: SecretStr,
        *,
        current_password: SecretStr | None = None,
    ) -> None:
        """Replace a password and end browser sessions; HTTP callers must supply the old password."""
        password = NewPasswordAdapter.validate_python(password)
        async with self.lock(username), async_session() as session:
            user = await session.one_or_none(
                User, expressions=[User["username"] == username]
            )
            if user is None or user.password_hash is None:
                raise InvalidCredentials
            if current_password is not None:
                try:
                    await to_thread.run_sync(
                        self.password_hasher.verify,
                        user.password_hash.get_secret_value(),
                        current_password.get_secret_value(),
                        limiter=self.password_limiter,
                    )
                except VerifyMismatchError as error:
                    raise InvalidCredentials from error
            user.password_hash = await self.hash_password(password)
            now = datetime.now(UTC)
            for current in await session.list(
                UserSession,
                expressions=[
                    UserSession["user_id"] == user.id,
                    UserSession["revoked_at"].is_(None),
                ],
                limit=None,
            ):
                current.revoked_at = now
            await session.commit()

    async def logout(
        self, access_token: SecretStr | None, refresh_token: SecretStr | None
    ) -> None:
        expressions = []
        if access_token is not None:
            expressions.append(
                UserSession["access_token_hash"]
                == self.hash_token(access_token, self.config.access_token_salt)
            )
        if refresh_token is not None:
            expressions.append(
                UserSession["refresh_token_hash"]
                == self.hash_token(refresh_token, self.config.refresh_token_salt)
            )
        if not expressions:
            return
        async with async_session() as session:
            for current in await session.list(
                UserSession,
                expressions=[or_(*expressions), UserSession["revoked_at"].is_(None)],
                limit=None,
            ):
                current.revoked_at = datetime.now(UTC)
            await session.commit()

    async def refresh_session(self, token: SecretStr) -> SessionTokens:
        now = datetime.now(UTC)
        async with async_session() as session:
            current = await session.one_or_none(
                UserSession,
                expressions=[
                    UserSession["refresh_token_hash"]
                    == self.hash_token(token, self.config.refresh_token_salt),
                    UserSession["revoked_at"].is_(None),
                    UserSession["refresh_expires_at"] > now,
                ],
            )
            if current is None:
                raise InvalidCredentials
            deadline = current.refresh_expires_at
            tokens = SessionTokens(
                session_id=current.id,
                user_id=current.user_id,
                access_token=SecretStr(secrets.token_urlsafe(32)),
                refresh_token=SecretStr(secrets.token_urlsafe(32)),
                access_expires_at=min(
                    now + self.config.access_token_lifetime, deadline
                ),
                refresh_expires_at=deadline,
            )
            current.access_token_hash = self.hash_token(
                tokens.access_token, self.config.access_token_salt
            )
            current.refresh_token_hash = self.hash_token(
                tokens.refresh_token, self.config.refresh_token_salt
            )
            current.access_expires_at = tokens.access_expires_at
            try:
                await session.flush()
            except StaleDataError as error:
                raise InvalidCredentials from error
            await session.commit()
            return tokens

    async def revoke_session(self, user_id: UUID7, session_id: UUID7) -> None:
        async with async_session() as session:
            current = await session.get(UserSession, session_id)
            if current is None or current.user_id != user_id:
                raise InvalidCredentials
            if current.revoked_at is None:
                current.revoked_at = datetime.now(UTC)
                try:
                    await session.flush()
                except StaleDataError as error:
                    raise InvalidCredentials from error
                await session.commit()

    async def create_api_key(
        self,
        user_id: UUID7,
        *,
        name: str,
        scopes: list[ApiKeyScope],
        expires_at: AwareDatetime | None = None,
    ) -> UserApiKey:
        """Issue for a verified user; the HTTP caller must establish that identity."""
        token = SecretStr(self.config.api_key_prefix + secrets.token_urlsafe(32))
        key = UserApiKey(
            user_id=user_id,
            name=name,
            scopes=scopes,
            expires_at=expires_at,
            key_prefix=token.get_secret_value()[:12],
            key_hash=self.hash_token(token, self.config.api_key_salt),
        )
        if key.expires_at is not None and key.expires_at <= datetime.now(UTC):
            raise ValueError("API key expiry must be in the future")
        async with async_session() as session:
            if await session.get(User, user_id) is None:
                raise InvalidCredentials
            session.add(key)
            await session.commit()
        key.bearer = token
        return key

    async def authenticate_api_key(
        self, token: SecretStr, *, scope: ApiKeyScope
    ) -> ApiKey | None:
        token_hash = self.hash_token(token, self.config.api_key_salt)
        async with async_session() as session:
            now = datetime.now(UTC)
            key = await session.one_or_none(
                ApiKey,
                expressions=[
                    ApiKey["key_hash"] == token_hash,
                    ApiKey["revoked_at"].is_(None),
                    ApiKey["expires_at"].is_(None) | (ApiKey["expires_at"] > now),
                ],
            )
            if key is None or scope not in key.scopes:
                return None
            if not isinstance(key, RuntimeApiKey):
                return key
        async with self.lock(f"api-key:{key.user_id}"), async_session() as session:
            if session.get_bind().dialect.name == "sqlite":
                # SQLite has no row locks; reserve its writer before reading.
                await session.execute(text("BEGIN IMMEDIATE"))
            await session.get(User, key.user_id, with_for_update=True)
            current = await session.get(RuntimeApiKey, key.id)
            now = datetime.now(UTC)
            if (
                current is None
                or current.key_hash != token_hash
                or current.revoked_at is not None
                or (current.expires_at is not None and current.expires_at <= now)
                or scope not in current.scopes
            ):
                return None
            current.expires_at = max(
                current.expires_at or now, now + self.config.runtime_api_key_lifetime
            )
            await session.commit()
            return current

    def runtime_bearer(self, nonce: UUID7) -> SecretStr:
        """Reproduce a runtime API key without storing its bearer in the database."""
        digest = hmac.digest(
            self.config.api_key_salt.get_secret_value().encode(),
            b"octomate:runtime-api-key:v1:" + nonce.bytes,
            "sha256",
        ).hex()
        return SecretStr(self.config.api_key_prefix + digest)

    async def runtime_api_key(
        self, user_id: UUID7, *, agent_id: str, scopes: list[ApiKeyScope]
    ) -> RuntimeApiKey:
        """Reuse shared persisted state; expiry rotates the bearer, not the key row."""
        scopes = sorted(set(scopes))
        async with self.lock(f"api-key:{user_id}"), async_session() as session:
            if session.get_bind().dialect.name == "sqlite":
                # SQLite has no row locks; reserve its writer before reading.
                await session.execute(text("BEGIN IMMEDIATE"))
            if await session.get(User, user_id, with_for_update=True) is None:
                raise InvalidCredentials
            key = await session.one_or_none(
                RuntimeApiKey,
                expressions=[
                    RuntimeApiKey["user_id"] == user_id,
                    RuntimeApiKey["runtime_agent_id"] == agent_id,
                ],
            )
            now = datetime.now(UTC)
            deadline = now + self.config.runtime_api_key_lifetime
            if key is not None and key.revoked_at is not None:
                raise InvalidCredentials
            if (
                key is None
                or key.expires_at is None
                or key.expires_at <= now
                or key.scopes != scopes
            ):
                nonce = uuid7()
            else:
                nonce = key.runtime_nonce
            token = self.runtime_bearer(nonce)
            if (
                key is not None
                and nonce == key.runtime_nonce
                and key.key_hash != self.hash_token(token, self.config.api_key_salt)
            ):
                raise ValueError(
                    "Runtime API key does not match this core's auth secrets"
                )
            if key is None:
                key = RuntimeApiKey(
                    user_id=user_id,
                    scopes=scopes,
                    runtime_agent_id=agent_id,
                    runtime_nonce=nonce,
                    key_hash=self.hash_token(token, self.config.api_key_salt),
                    key_prefix=token.get_secret_value()[:12],
                )
                session.add(key)
            key.runtime_nonce = nonce
            key.key_hash = self.hash_token(token, self.config.api_key_salt)
            key.key_prefix = token.get_secret_value()[:12]
            key.scopes = scopes
            key.expires_at = max(key.expires_at or now, deadline)
            await session.commit()
            key.bearer = token
            return key

    async def get_api_key(self, user_id: UUID7, key_id: UUID7) -> ApiKey:
        async with async_session() as session:
            key = await session.get(ApiKey, key_id)
            if key is None or key.user_id != user_id:
                raise InvalidCredentials
            return key

    async def list_api_keys(self, user_id: UUID7) -> list[UserApiKey]:
        async with async_session() as session:
            return list(
                await session.list(
                    UserApiKey,
                    expressions=[UserApiKey["user_id"] == user_id],
                    limit=None,
                )
            )

    async def revoke_api_key(self, user_id: UUID7, key_id: UUID7) -> None:
        async with self.lock(f"api-key:{user_id}"), async_session() as session:
            if session.get_bind().dialect.name == "sqlite":
                # SQLite has no row locks; reserve its writer before reading.
                await session.execute(text("BEGIN IMMEDIATE"))
            await session.get(User, user_id, with_for_update=True)
            key = await session.get(ApiKey, key_id)
            if key is None or key.user_id != user_id:
                raise InvalidCredentials
            if key.revoked_at is not None:
                return
            key.revoked_at = datetime.now(UTC)
            await session.commit()

    async def approve_oauth_authorization(
        self, user_id: UUID7, ticket: SecretStr
    ) -> OAuthAuthorization:
        """Bind pending consent to the authenticated browser user, once only."""
        ticket_hash = self.hash_token(ticket, self.config.access_token_salt)
        async with async_session() as session:
            pending = await session.one_or_none(
                OAuthAuthorization,
                expressions=[OAuthAuthorization["ticket_hash"] == ticket_hash],
            )
        if pending is None:
            raise InvalidCredentials
        async with self.lock(f"oauth-consent:{pending.id}"), async_session() as session:
            if session.get_bind().dialect.name == "sqlite":
                # SQLite has no row locks; reserve its writer before reading.
                await session.execute(text("BEGIN IMMEDIATE"))
            pending = await session.get(
                OAuthAuthorization, pending.id, with_for_update=True
            )
            now = datetime.now(UTC)
            if (
                pending is None
                or pending.ticket_hash != ticket_hash
                or pending.expires_at <= now
                or pending.grant_id is not None
            ):
                raise InvalidCredentials
            if await session.get(User, user_id) is None:
                raise InvalidCredentials
            client = await session.get(OAuthClient, pending.client_id)
            if client is None:
                raise InvalidCredentials
            if not pending.scopes or not set(pending.scopes) <= set(client.scopes):
                raise InvalidOAuthScopes
            grant = OAuthGrant(
                client_id=client.id,
                user_id=user_id,
                resource=pending.resource,
                scopes=sorted(set(pending.scopes)),
                expires_at=now + self.config.oauth_grant_idle_lifetime,
            )
            session.add(grant)
            await session.flush()
            code = SecretStr(secrets.token_urlsafe(32))
            pending.grant_id = grant.id
            pending.code_hash = self.hash_token(code, self.config.access_token_salt)
            pending.expires_at = min(
                grant.expires_at, now + self.config.oauth_authorization_lifetime
            )
            await session.commit()
            pending.code = code
            return pending

    async def exchange_oauth_code(
        self, client_id: UUID7, code: SecretStr, *, resource: str
    ) -> OAuthToken:
        """Consume a code after the OAuth handler verifies PKCE and redirect binding.

        The protocol handler owns request validation. Reload under the grant lock
        so expiry, replay, and revocation remain authoritative at issuance.
        """
        code_hash = self.hash_token(code, self.config.access_token_salt)
        async with async_session() as session:
            authorization = await session.one_or_none(
                OAuthAuthorization,
                expressions=[OAuthAuthorization["code_hash"] == code_hash],
            )
        if authorization is None or authorization.grant_id is None:
            raise InvalidCredentials
        async with (
            self.lock(f"oauth-grant:{authorization.grant_id}"),
            async_session() as session,
        ):
            if session.get_bind().dialect.name == "sqlite":
                await session.execute(text("BEGIN IMMEDIATE"))
            grant = await session.get(
                OAuthGrant, authorization.grant_id, with_for_update=True
            )
            authorization = await session.get(OAuthAuthorization, authorization.id)
            now = datetime.now(UTC)
            if (
                grant is None
                or grant.client_id != client_id
                or grant.resource != resource
                or grant.revoked_at is not None
                or grant.expires_at <= now
                or authorization is None
                or authorization.code_hash != code_hash
            ):
                raise InvalidCredentials
            if authorization.consumed_at is not None:
                grant.revoked_at = now
                await session.commit()
                raise InvalidCredentials
            if authorization.expires_at <= now:
                raise InvalidCredentials
            access = SecretStr(secrets.token_urlsafe(32))
            refresh = SecretStr(secrets.token_urlsafe(32))
            token = OAuthToken(
                grant_id=grant.id,
                access_token_hash=self.hash_token(
                    access, self.config.access_token_salt
                ),
                refresh_token_hash=self.hash_token(
                    refresh, self.config.refresh_token_salt
                ),
                scopes=grant.scopes,
                access_expires_at=min(
                    grant.expires_at, now + self.config.access_token_lifetime
                ),
            )
            authorization.consumed_at = now
            session.add(token)
            await session.commit()
            token.access_token = access
            token.refresh_token = refresh
            return token

    async def refresh_oauth_token(
        self,
        client_id: UUID7,
        refresh_token: SecretStr,
        *,
        resource: str,
        scopes: list[ApiKeyScope] | None = None,
    ) -> OAuthToken:
        """Rotate once; replay revokes the entire grant, including newer tokens."""
        token_hash = self.hash_token(refresh_token, self.config.refresh_token_salt)
        async with async_session() as session:
            previous = await session.one_or_none(
                OAuthToken,
                expressions=[OAuthToken["refresh_token_hash"] == token_hash],
            )
        if previous is None:
            raise InvalidCredentials
        async with (
            self.lock(f"oauth-grant:{previous.grant_id}"),
            async_session() as session,
        ):
            if session.get_bind().dialect.name == "sqlite":
                await session.execute(text("BEGIN IMMEDIATE"))
            grant = await session.get(
                OAuthGrant, previous.grant_id, with_for_update=True
            )
            previous = await session.get(OAuthToken, previous.id)
            now = datetime.now(UTC)
            if (
                grant is None
                or grant.client_id != client_id
                or grant.resource != resource
                or grant.revoked_at is not None
                or grant.expires_at <= now
                or previous is None
                or previous.refresh_token_hash != token_hash
            ):
                raise InvalidCredentials
            if previous.refresh_used_at is not None:
                grant.revoked_at = now
                await session.commit()
                raise InvalidCredentials
            allowed = set(previous.scopes) & set(grant.scopes)
            requested = sorted(set(previous.scopes if scopes is None else scopes))
            if not requested or not set(requested) <= allowed:
                raise InvalidOAuthScopes
            grant.expires_at = now + self.config.oauth_grant_idle_lifetime
            access = SecretStr(secrets.token_urlsafe(32))
            refresh = SecretStr(secrets.token_urlsafe(32))
            token = OAuthToken(
                grant_id=grant.id,
                access_token_hash=self.hash_token(
                    access, self.config.access_token_salt
                ),
                refresh_token_hash=self.hash_token(
                    refresh, self.config.refresh_token_salt
                ),
                scopes=requested,
                access_expires_at=min(
                    grant.expires_at, now + self.config.access_token_lifetime
                ),
            )
            previous.refresh_used_at = now
            session.add(token)
            await session.commit()
            token.access_token = access
            token.refresh_token = refresh
            return token

    async def authenticate_oauth_token(
        self, access_token: SecretStr, *, resource: str, scope: ApiKeyScope
    ) -> OAuthPrincipal | None:
        """Check the token and its persisted grant on every resource request."""
        async with async_session() as session:
            token = await session.one_or_none(
                OAuthToken,
                expressions=[
                    OAuthToken["access_token_hash"]
                    == self.hash_token(access_token, self.config.access_token_salt)
                ],
            )
            if token is None:
                return None
            grant = await session.get(OAuthGrant, token.grant_id)
            now = datetime.now(UTC)
            if (
                grant is None
                or grant.resource != resource
                or grant.revoked_at is not None
                or grant.expires_at <= now
                or token.access_expires_at <= now
                or scope not in token.scopes
                or scope not in grant.scopes
            ):
                return None
            return OAuthPrincipal(
                grant_id=grant.id,
                user_id=grant.user_id,
                client_id=grant.client_id,
                resource=grant.resource,
                scopes=sorted(set(token.scopes) & set(grant.scopes)),
                expires_at=min(grant.expires_at, token.access_expires_at),
            )

    async def revoke_oauth_grant(self, user_id: UUID7, grant_id: UUID7) -> None:
        """Revoke one owned grant without affecting another installation's consent."""
        async with self.lock(f"oauth-grant:{grant_id}"), async_session() as session:
            if session.get_bind().dialect.name == "sqlite":
                await session.execute(text("BEGIN IMMEDIATE"))
            grant = await session.get(OAuthGrant, grant_id, with_for_update=True)
            if grant is None or grant.user_id != user_id:
                raise InvalidCredentials
            if grant.revoked_at is not None:
                return
            grant.revoked_at = datetime.now(UTC)
            await session.commit()
