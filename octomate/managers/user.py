"""The cross-channel user registry: users, the channel profiles they own, and
profile linking."""

from __future__ import annotations

import hashlib
import secrets
import uuid
from datetime import UTC, datetime, timedelta

from pydantic import AnyHttpUrl, SecretStr
from sqlalchemy.orm.exc import StaleDataError

from octomate.database import async_session
from octomate.managers.base import Locks, Manager
from octomate.schemas.auth import (
    LinkProfileAuthorization,
    LinkProfileInfo,
    LinkProfileSession,
)
from octomate.schemas.user import User, UserProfile

PROFILE_FIELDS = {"name", "nickname", "gender", "age", "title"}


class LinkProfileUnavailable(RuntimeError):
    """Profile linking cannot start: local auth or the callback origin is not
    configured."""


class InvalidLinkProfile(ValueError):
    """A profile-link ticket that is invalid, expired or already used."""

    def __init__(self) -> None:
        super().__init__("This profile link is invalid, expired, or already used")


class ProfileAlreadyLinked(ValueError):
    """The channel profile is already linked to an account."""

    def __init__(self) -> None:
        super().__init__("This channel profile is already linked")


class ProfileNotLinked(ValueError):
    """The profile is not linked to the requesting account."""

    def __init__(self) -> None:
        super().__init__("This profile is not linked to your account")


class UserManager(Manager, Locks[tuple[str, str] | uuid.UUID]):
    """The persisted cross-channel user registry and verified profile links."""

    def __init__(
        self,
        *,
        authorization_base_uri: AnyHttpUrl | None = None,
        authorization_lifetime: timedelta = timedelta(minutes=10),
    ) -> None:
        self.authorization_base_uri = authorization_base_uri
        self.authorization_lifetime = authorization_lifetime

    async def unlink_profile(self, user: User, profile_id: uuid.UUID) -> None:
        async with self.lock(profile_id), async_session() as session:
            profile = await session.get(UserProfile, profile_id)
            if profile is None or profile.user_id != user.id:
                raise ProfileNotLinked
            profile.user_id = None
            pending = await session.one_or_none(
                LinkProfileSession,
                expressions=[LinkProfileSession["profile_id"] == profile_id],
            )
            if pending is not None:
                pending.consumed_at = datetime.now(UTC)
            await session.commit()

    async def owner(self, profile: UserProfile) -> User | None:
        """Return the registered owner of ``profile``, or ``None`` for a visitor."""
        user_id = profile.user_id
        if user_id is None:
            return None
        async with async_session() as session:
            user = await session.get(User, user_id)
        if user is None:
            raise ValueError(f"unknown user {user_id}")
        return user

    async def linked_profiles(
        self,
        profile: UserProfile,
    ) -> list[UserProfile]:
        """This human's other channel identities — the same person on another
        platform, reachable precisely because the registry links the accounts.

        Empty for a visitor: only a registered `User` links identities, so an
        unregistered account is one account and can be followed nowhere. `owner`
        answers "whose is this"; this answers "where else are they".
        """
        user = await self.owner(profile)
        if user is None:
            return []
        async with async_session() as session:
            return list(
                await session.list(
                    UserProfile,
                    limit=None,
                    expressions=[
                        UserProfile["user_id"] == user.id,
                        UserProfile["channel_tentacle_id"]
                        != profile.channel_tentacle_id,
                    ],
                )
            )

    async def native_profile(self, runtime: str, username: str) -> UserProfile | None:
        """A transient anchor for `username`'s native session on `runtime`'s
        pseudo-channel, or None for an unknown username.

        Never persisted: a native session's identity comes from its verified
        bearer, not from a claimed row, so the profile exists only to give the
        linked-profile walk its starting point — owned like a stored profile,
        and standing on a channel id no channel ever resolves.
        """
        async with async_session() as session:
            user = await session.one_or_none(
                User, expressions=[User["username"] == username]
            )
        if user is None:
            return None
        return UserProfile(
            channel_tentacle_id=runtime,
            channel_user_id=username,
            user_id=user.id,
            name=user.name,
            nickname=user.nickname,
        )

    async def profile(
        self, channel_tentacle_id: str, channel_user_id: str
    ) -> UserProfile | None:
        """The stored profile for a channel identity, or ``None`` if never observed.

        The read-only sibling of ``ensure_profile``, for callers that hold only a
        channel identity — e.g. a deferred batch resuming long after the message
        that carried the sender's profile snapshot."""
        async with async_session() as session:
            return await session.one_or_none(
                UserProfile,
                expressions=[
                    UserProfile["channel_tentacle_id"] == channel_tentacle_id,
                    UserProfile["channel_user_id"] == channel_user_id,
                ],
            )

    async def ensure_profile(
        self, channel_tentacle_id: str, observed: UserProfile
    ) -> UserProfile:
        """Persist the latest channel snapshot and return its registry profile.

        First sight creates an ownerless visitor profile. An existing profile
        keeps its ownership while observations refresh its display fields. An
        `observed.user_id` is the one exception: no channel ever sets it —
        only a verified bearer's transient anchor (`native_profile`) carries
        one — so an identity that arrives owned stays owned.
        """
        key = (channel_tentacle_id, observed.channel_user_id)
        async with self.lock(key), async_session() as session:
            profile = await session.one_or_none(
                UserProfile,
                expressions=[
                    UserProfile["channel_tentacle_id"] == channel_tentacle_id,
                    UserProfile["channel_user_id"] == observed.channel_user_id,
                ],
            )
            if profile is None:
                profile = UserProfile(
                    channel_tentacle_id=channel_tentacle_id,
                    channel_user_id=observed.channel_user_id,
                    user_id=observed.user_id,
                    **observed.model_dump(include=PROFILE_FIELDS),
                )
                session.add(profile)
            else:
                for name in PROFILE_FIELDS:
                    setattr(profile, name, getattr(observed, name))
                if observed.user_id is not None:
                    profile.user_id = observed.user_id

            await session.commit()

        return profile

    @staticmethod
    def hash_link_token(token: SecretStr) -> SecretStr:
        return SecretStr(hashlib.sha256(token.get_secret_value().encode()).hexdigest())

    async def link_verified_profile(self, profile: UserProfile, user: User) -> None:
        """Link an OAuth-verified profile without replacing an existing owner."""
        async with self.lock(profile.id), async_session() as session:
            stored = await session.get(UserProfile, profile.id)
            if stored is None or await session.get(User, user.id) is None:
                raise InvalidLinkProfile
            if stored.user_id is not None and stored.user_id != user.id:
                raise ProfileAlreadyLinked
            stored.user_id = user.id
            pending = await session.one_or_none(
                LinkProfileSession,
                expressions=[LinkProfileSession["profile_id"] == stored.id],
            )
            if pending is not None:
                pending.consumed_at = datetime.now(UTC)
            await session.commit()

    async def start_link_profile(
        self, profile: UserProfile
    ) -> LinkProfileAuthorization:
        """Replace this ownerless profile's pending ticket and return its private URL."""
        if self.authorization_base_uri is None:
            raise LinkProfileUnavailable(
                "Configure local auth and oauth.callback_base_uri before linking channel profiles"
            )
        token = SecretStr(secrets.token_urlsafe(32))
        now = datetime.now(UTC)
        async with self.lock(profile.id), async_session() as session:
            stored = await session.get(UserProfile, profile.id)
            if stored is None:
                raise InvalidLinkProfile
            if stored.user_id is not None:
                raise ProfileAlreadyLinked
            pending = await session.one_or_none(
                LinkProfileSession,
                expressions=[LinkProfileSession["profile_id"] == stored.id],
            )
            if pending is None:
                pending = LinkProfileSession(
                    profile_id=stored.id,
                    token_hash=self.hash_link_token(token),
                    expires_at=now + self.authorization_lifetime,
                )
                session.add(pending)
            else:
                pending.token_hash = self.hash_link_token(token)
                pending.expires_at = now + self.authorization_lifetime
                pending.created_at = now
                pending.consumed_at = None
            await session.commit()

        root = str(self.authorization_base_uri).rstrip("/")
        return LinkProfileAuthorization(
            profile=stored,
            authorization_uri=AnyHttpUrl(
                f"{root}/#link-profile={token.get_secret_value()}"
            ),
            expires_at=now + self.authorization_lifetime,
        )

    async def inspect_link_profile(self, token: SecretStr) -> LinkProfileInfo:
        token_hash = self.hash_link_token(token)
        async with async_session() as session:
            pending = await session.one_or_none(
                LinkProfileSession,
                expressions=[
                    LinkProfileSession["token_hash"] == token_hash,
                    LinkProfileSession["consumed_at"].is_(None),
                    LinkProfileSession["expires_at"] > datetime.now(UTC),
                ],
            )
            if pending is None:
                raise InvalidLinkProfile
            profile = await session.get(UserProfile, pending.profile_id)
            if profile is None:
                raise InvalidLinkProfile
            if profile.user_id is not None:
                raise ProfileAlreadyLinked
            return LinkProfileInfo(profile=profile, expires_at=pending.expires_at)

    async def confirm_link_profile(self, token: SecretStr, user: User) -> UserProfile:
        """Consume a valid ticket and link its exact profile to ``user`` once."""
        token_hash = self.hash_link_token(token)
        async with async_session() as session:
            found = await session.one_or_none(
                LinkProfileSession,
                expressions=[LinkProfileSession["token_hash"] == token_hash],
            )
        if found is None:
            raise InvalidLinkProfile

        async with self.lock(found.profile_id), async_session() as session:
            pending = await session.one_or_none(
                LinkProfileSession,
                expressions=[
                    LinkProfileSession["token_hash"] == token_hash,
                    LinkProfileSession["consumed_at"].is_(None),
                    LinkProfileSession["expires_at"] > datetime.now(UTC),
                ],
            )
            if pending is None:
                raise InvalidLinkProfile
            profile = await session.get(UserProfile, pending.profile_id)
            if profile is None or await session.get(User, user.id) is None:
                raise InvalidLinkProfile
            if profile.user_id is not None:
                raise ProfileAlreadyLinked
            profile.user_id = user.id
            pending.consumed_at = datetime.now(UTC)
            try:
                await session.flush()
            except StaleDataError as error:
                raise InvalidLinkProfile from error
            await session.commit()
            return profile
