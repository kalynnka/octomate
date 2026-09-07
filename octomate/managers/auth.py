from __future__ import annotations

import hashlib
import secrets
import uuid
from datetime import UTC, datetime

from anyio import CapacityLimiter, to_thread
from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError
from pydantic import AwareDatetime, SecretStr
from sqlalchemy.orm.exc import StaleDataError
from uuid_utils.compat import uuid7

from octomate.config.auth import AuthConfig
from octomate.database import async_session
from octomate.schemas.auth import IssuedApiKey, SessionTokens, UserApiKey, UserSession
from octomate.schemas.user import User
from octomate.types.auth import ApiKeyScope


class InvalidCredentials(ValueError):
    def __init__(self) -> None:
        super().__init__("Invalid or expired credentials")


class AuthManager:
    """Local passwords and credentials; callers own admission and resource access."""

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
                await session.commit()
            except StaleDataError as error:
                raise InvalidCredentials from error
            return tokens

    async def revoke_session(self, user_id: uuid.UUID, session_id: uuid.UUID) -> None:
        async with async_session() as session:
            current = await session.get(UserSession, session_id)
            if current is None or current.user_id != user_id:
                raise InvalidCredentials
            if current.revoked_at is None:
                current.revoked_at = datetime.now(UTC)
                try:
                    await session.commit()
                except StaleDataError as error:
                    raise InvalidCredentials from error

    async def create_api_key(
        self,
        user_id: uuid.UUID,
        *,
        name: str,
        scopes: list[ApiKeyScope],
        expires_at: AwareDatetime | None = None,
    ) -> IssuedApiKey:
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
        return IssuedApiKey(key=key, token=token)

    async def authenticate_api_key(
        self, token: SecretStr, *, scope: ApiKeyScope
    ) -> UserApiKey | None:
        async with async_session() as session:
            key = await session.one_or_none(
                UserApiKey,
                expressions=[
                    UserApiKey["key_hash"]
                    == self.hash_token(token, self.config.api_key_salt),
                    UserApiKey["revoked_at"].is_(None),
                    UserApiKey["expires_at"].is_(None)
                    | (UserApiKey["expires_at"] > datetime.now(UTC)),
                ],
            )
        return key if key is not None and scope in key.scopes else None

    async def revoke_api_key(self, user_id: uuid.UUID, key_id: uuid.UUID) -> None:
        async with async_session() as session:
            key = await session.get(UserApiKey, key_id)
            if key is None or key.user_id != user_id:
                raise InvalidCredentials
            if key.revoked_at is None:
                key.revoked_at = datetime.now(UTC)
                await session.commit()
