from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timezone

from octomate.config.users import UserConfig
from octomate.database import async_session
from octomate.schemas.user import User, UserLinkMethod, UserProfile

# The observed per-channel profile shape, refreshed from boundary snapshots.
PROFILE_FIELDS = ("name", "nickname", "gender", "age", "title")


class UserManager:
    """The cross-channel identity registry: `User` rows and their per-channel
    `UserProfile` rows, the link being the profile's nullable owner.

    Unlike `ConversationManager`'s LRU, this holds the whole registry in
    memory — `resolve` sits on the ingest hot path and the registry is small: a
    handful of humans plus one profile row per platform account ever seen.
    `load()` fills the maps once at startup; writes keep them coherent after
    each commit.
    """

    def __init__(self, config: dict[str, UserConfig] | None = None) -> None:
        # The `users:` section of octomate.yaml — the authority reconcile applies.
        self.config = config or {}
        self.users: dict[uuid.UUID, User] = {}
        self.handles: dict[str, User] = {}
        self.profiles: dict[tuple[str, str], UserProfile] = {}
        self.loaded = False
        # Serializes first sightings, the ConversationManager.ensure pattern:
        # two concurrent ingests of one new account must not both insert.
        self.ensure_lock = asyncio.Lock()

    async def load(self) -> None:
        async with async_session() as session:
            users = await session.list(User, limit=None)
            profiles = await session.list(UserProfile, limit=None)
        self.users = {user.id: user for user in users}
        self.handles = {user.handle: user for user in users}
        self.profiles = {
            (profile.channel_tentacle_id, profile.channel_user_id): profile
            for profile in profiles
        }
        self.loaded = True

    async def ensure_profile(
        self, channel_tentacle_id: str, profile: UserProfile
    ) -> UserProfile:
        """The registry row for a sender: created on first sight, profile
        fields refreshed when the snapshot changed. `profile` is an ephemeral
        boundary instance (often a channel subclass); the returned row is the
        session-bound registry copy."""
        if not self.loaded:
            await self.load()
        key = (channel_tentacle_id, profile.channel_user_id)
        row = self.profiles.get(key)
        if row is None:
            async with self.ensure_lock:
                row = self.profiles.get(key)
                if row is not None:
                    return row
                row = UserProfile(
                    channel_tentacle_id=channel_tentacle_id,
                    channel_user_id=profile.channel_user_id,
                    **{name: getattr(profile, name) for name in PROFILE_FIELDS},
                )
                async with async_session() as session:
                    session.add(row)
                    await session.commit()
                self.profiles[key] = row
                return row
        changes = {
            name: getattr(profile, name)
            for name in PROFILE_FIELDS
            if getattr(profile, name) != getattr(row, name)
        }
        if changes:
            async with async_session() as session:
                stored = await session.get(UserProfile, row.id)
                if stored is None:
                    raise RuntimeError(f"profile row {row.id} vanished under refresh")
                for name, value in changes.items():
                    setattr(stored, name, value)
                await session.commit()
            for name, value in changes.items():
                setattr(row, name, value)
        return row

    def resolve(
        self, channel_tentacle_id: str, channel_user_id: str
    ) -> UserProfile | None:
        """The registry row for a platform account, or None if never seen.
        A row with `user_id=None` is an observation, not a linked identity.
        Sync, so it cannot lazy-load: an unloaded registry raises instead of
        silently missing everyone."""
        if not self.loaded:
            raise RuntimeError("UserManager.load() must run before resolve()")
        return self.profiles.get((channel_tentacle_id, channel_user_id))

    def owner_of(self, profile: UserProfile) -> User | None:
        if profile.user_id is None:
            return None
        return self.users.get(profile.user_id)

    async def link(
        self,
        user: User,
        channel_tentacle_id: str,
        channel_user_id: str,
        *,
        method: UserLinkMethod,
    ) -> UserProfile:
        """Link a platform account to `user`, creating a bare profile row for an
        account never yet seen. Fails fast if the account is already linked to a
        different user — no silent re-link (the false-merge hazard); already
        linked to the same user with the same method is a no-op."""
        key = (channel_tentacle_id, channel_user_id)
        profile = self.profiles.get(key)
        if profile is not None and profile.user_id not in (None, user.id):
            other = self.users.get(profile.user_id)
            raise ValueError(
                f"{channel_tentacle_id}:{channel_user_id} is already linked to user "
                f"{other.handle if other else profile.user_id!s}; unlink it first"
            )
        if (
            profile is not None
            and profile.user_id == user.id
            and profile.method == method
        ):
            return profile
        verified_at = datetime.now(timezone.utc)
        async with async_session() as session:
            if profile is None:
                profile = UserProfile(
                    channel_tentacle_id=channel_tentacle_id,
                    channel_user_id=channel_user_id,
                    user_id=user.id,
                    method=method,
                    verified_at=verified_at,
                )
                session.add(profile)
                await session.commit()
                self.profiles[key] = profile
                return profile
            stored = await session.get(UserProfile, profile.id)
            if stored is None:
                raise RuntimeError(f"profile row {profile.id} vanished under link")
            stored.user_id = user.id
            stored.method = method
            stored.verified_at = verified_at
            await session.commit()
        profile.user_id = user.id
        profile.method = method
        profile.verified_at = verified_at
        return profile

    async def unlink(
        self, channel_tentacle_id: str, channel_user_id: str
    ) -> UserProfile:
        """NULL the link and keep the row — the profile stays an observation."""
        profile = self.profiles.get((channel_tentacle_id, channel_user_id))
        if profile is None:
            raise ValueError(f"no profile for {channel_tentacle_id}:{channel_user_id}")
        if profile.user_id is None:
            return profile
        async with async_session() as session:
            stored = await session.get(UserProfile, profile.id)
            if stored is None:
                raise RuntimeError(f"profile row {profile.id} vanished under unlink")
            stored.user_id = None
            stored.method = None
            stored.verified_at = None
            await session.commit()
        profile.user_id = None
        profile.method = None
        profile.verified_at = None
        return profile

    async def reconcile(self) -> None:
        """Startup sync of the `users:` config. Config is the authority for
        everything it declares: users are upserted by handle and deleted when
        removed (their profiles unlink — no user, no link, whatever the
        method); `method="config"` links follow the declared set exactly.
        Handshake links of surviving users are never touched. A declared link
        that collides with another user's handshake link fails the boot loudly
        via `link`'s fail-fast."""
        if not self.loaded:
            raise RuntimeError("UserManager.reconcile() requires load() first")

        for handle in [h for h in self.handles if h not in self.config]:
            user = self.handles[handle]
            for key, profile in self.profiles.items():
                if profile.user_id == user.id:
                    await self.unlink(*key)
            async with async_session() as session:
                stored = await session.get(User, user.id)
                if stored is not None:
                    await session.delete(stored)
                    await session.commit()
            del self.handles[handle]
            del self.users[user.id]

        for handle, user_config in self.config.items():
            user = self.handles.get(handle)
            if user is None:
                user = User(handle=handle, name=user_config.name)
                async with async_session() as session:
                    session.add(user)
                    await session.commit()
                self.users[user.id] = user
                self.handles[handle] = user
            elif user.name != user_config.name:
                async with async_session() as session:
                    stored = await session.get(User, user.id)
                    if stored is None:
                        raise RuntimeError(f"user row {user.id} vanished")
                    stored.name = user_config.name
                    await session.commit()
                user.name = user_config.name

        declared = {
            (channel_id, config_profile.channel_user_id): (handle, config_profile)
            for handle, user_config in self.config.items()
            for channel_id, config_profile in user_config.profiles.items()
        }
        # Drop config links the config no longer declares — including an account
        # the config moved to another user, which must unlink before it relinks.
        for key, profile in self.profiles.items():
            if profile.method != "config" or profile.user_id is None:
                continue
            owner = self.users.get(profile.user_id)
            declared_for = declared.get(key)
            if owner is None or declared_for is None or declared_for[0] != owner.handle:
                await self.unlink(*key)
        for key, (handle, config_profile) in declared.items():
            # Config profile fields seed a never-seen account; an existing row's
            # observed fields are never overwritten from config.
            if self.profiles.get(key) is None:
                await self.ensure_profile(key[0], config_profile)
            await self.link(self.handles[handle], *key, method="config")
