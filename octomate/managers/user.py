from __future__ import annotations

import asyncio
import uuid

from pydantic import SecretStr

from octomate.config.users import UsersConfig
from octomate.database import async_session
from octomate.schemas.user import User, UserProfile
from octomate.types.threads import NATIVE_TENTACLE_IDS

PROFILE_FIELDS = {"name", "nickname", "gender", "age", "title"}


class UserManager:
    """The cross-channel user registry, currently configured through YAML.

    A persisted profile is an observed channel identity. It belongs to a
    registered ``User`` only when the ``users:`` YAML config declares that
    exact channel account; every other profile is a visitor with no owner.

    Profiles are queried by their indexed channel identity. Users are expected
    to remain a small registry and are all cached for profile-to-owner lookup.
    """

    def __init__(self, config: UsersConfig | None = None) -> None:
        self.config = config if config is not None else UsersConfig()
        self.users: dict[uuid.UUID, User] = {}
        self.ensure_lock = asyncio.Lock()

    def cache_user(self, user: User) -> None:
        self.users[user.id] = user

    async def owner(self, profile: UserProfile) -> User | None:
        """Return the registered owner of ``profile``, or ``None`` for a visitor."""
        if (user_id := profile.user_id) is None:
            return None
        if (cached := self.users.get(user_id)) is not None:
            return cached
        if (related := profile.user.peek()) is not None:
            self.cache_user(related)
            return related
        async with async_session() as session:
            user = await session.get(User, user_id)
        if user is None:
            raise ValueError(f"unknown user {user_id}")
        self.cache_user(user)
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
        # Sessions do not expire on commit, so a user cached with its profiles still
        # has them and `peek` answers without IO. It returns None only when the
        # relation was never loaded — touching it then would lazy-load a detached
        # instance into a DetachedInstanceError, so query instead.
        if user.profiles.peek() is None:
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
        return [
            other
            for other in user.profiles
            if other.channel_tentacle_id != profile.channel_tentacle_id
        ]

    async def secret_of(self, profile: UserProfile | None) -> SecretStr | None:
        """The bearer credential `profile`'s registered owner carries on their
        registry row, or None — for no profile, a visitor, or an owner whose
        row holds no secret.

        What a driven turn's gateway wiring resolves: the turn speaks with its
        kicker's own credential or not at all, since every configured credential
        names a person and the host holds none of its own."""
        if profile is None:
            return None
        user = await self.owner(profile)
        return user.secret if user is not None else None

    async def native_profile(self, runtime: str, username: str) -> UserProfile | None:
        """A transient anchor for `username`'s native session on `runtime`'s
        pseudo-channel, or None for a username the registry never reconciled.

        Never persisted: a native session's identity comes from its verified
        bearer, not from a claimed row, so the profile exists only to give the
        linked-profile walk its starting point — owned like a stored profile,
        and standing on a channel id no channel ever resolves.
        """
        user = next(
            (cached for cached in self.users.values() if cached.username == username),
            None,
        )
        if user is None:
            async with async_session() as session:
                user = await session.one_or_none(
                    User, expressions=[User["username"] == username]
                )
            if user is None:
                return None
            self.cache_user(user)
        # `user_id` alone carries the ownership: `owner()` resolves it through the
        # cache, and assigning the relation itself would backpopulate
        # `user.profiles` — a lazy load the detached cached instance cannot do.
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

        First sight creates an ownerless visitor profile. YAML reconciliation
        may already have seeded and attached the profile; observations refresh
        only its channel-owned display fields and never change ownership. An
        `observed.user_id` is the one exception: no channel ever sets it —
        only a verified bearer's transient anchor (`native_profile`) carries
        one — so an identity that arrives owned stays owned.
        """
        async with self.ensure_lock, async_session() as session:
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

            owner = await profile.user
            await session.commit()

        if owner is not None:
            self.cache_user(owner)
        return profile

    async def reconcile(self) -> None:
        """Make persisted users and profile ownership exactly match YAML.

        The YAML key is the user's stable username. Users absent from YAML are
        retained for future registration sources, while their undeclared profiles
        become visitors. Config profile details seed an unseen account but never
        overwrite an observed row. Native pseudo-channel profiles are the one
        exception to YAML's authority: their ownership came from a verified
        bearer at ingest, so it is re-anchored on the username row and drops
        only when that user leaves the registry.
        """
        declared: dict[tuple[str, str], str] = {}
        for username, user_config in self.config.items():
            for channel_id, config_profile in user_config.profiles.items():
                key = (channel_id, config_profile.channel_user_id)
                if (claimed_by := declared.get(key)) is not None:
                    raise ValueError(
                        f"{channel_id}:{config_profile.channel_user_id} is declared "
                        f"for both {claimed_by!r} and {username!r}"
                    )
                declared[key] = username

        async with async_session() as session:
            stored_users = list(await session.list(User, limit=None))
            users_by_username = {user.username: user for user in stored_users}

            for username, user_config in self.config.items():
                name = user_config.name or username
                # The row is the credential's home — the unique column is what
                # makes a bearer name exactly one user, tripping the boot here
                # when two entries share a value.
                secret = user_config.secret
                user = users_by_username.get(username)
                if user is None:
                    user = User(
                        username=username,
                        name=name,
                        nickname=user_config.nickname,
                        secret=secret,
                    )
                    session.add(user)
                    users_by_username[username] = user
                else:
                    user.name = name
                    user.nickname = user_config.nickname
                    user.secret = secret

            linked_profiles = list(
                await session.list(
                    UserProfile,
                    expressions=[UserProfile["user_id"].is_not(None)],
                    limit=None,
                )
            )
            profiles_by_key = {
                (profile.channel_tentacle_id, profile.channel_user_id): profile
                for profile in linked_profiles
            }

            for key, profile in profiles_by_key.items():
                channel_id, channel_user_id = key
                if channel_id in NATIVE_TENTACLE_IDS:
                    # A native profile's owner is its verified bearer, written at
                    # ingest — YAML cannot declare it (`validate_user_links`
                    # refuses pseudo-channels), so ownership follows the username
                    # row it stands on rather than the declarations.
                    username = channel_user_id
                else:
                    username = declared.get(key)
                if username is None or username not in users_by_username:
                    profile.user_id = None
                    profile.user.value = None
                    continue
                owner = users_by_username[username]
                profile.user_id = owner.id
                profile.user.value = owner

            # Every declared profile no linked row already covers — `declared`
            # already holds them all from the duplicate check above. An unlinked row
            # may still exist for one, an observed visitor this config now claims,
            # so they are read together and then written. Collecting before reading
            # and writing after is what keeps it to a single query: doing all three
            # in one pass is a read per declaration. Read-then-write rather than an
            # upsert because an upsert is only spelled per dialect, and reconcile
            # runs once at boot with nothing else writing, so there is no race.
            unclaimed = {
                key: username
                for key, username in declared.items()
                if key not in profiles_by_key
            }
            if unclaimed:
                # Each half of the key gets its own `IN` and the pair is matched in
                # Python: a composite `IN` is not portable, and two are still one
                # round trip.
                observed = {
                    (row.channel_tentacle_id, row.channel_user_id): row
                    for row in await session.list(
                        UserProfile,
                        limit=None,
                        expressions=[
                            UserProfile["channel_tentacle_id"].in_(
                                sorted({channel for channel, _ in unclaimed})
                            ),
                            UserProfile["channel_user_id"].in_(
                                sorted({account for _, account in unclaimed})
                            ),
                        ],
                    )
                }
                for key, username in unclaimed.items():
                    channel_id, _ = key
                    owner = users_by_username[username]
                    claimed = observed.get(key)
                    if claimed is not None:
                        claimed.user_id = owner.id
                        continue
                    config_profile = self.config[username].profiles[channel_id]
                    session.add(
                        UserProfile(
                            **config_profile.model_dump(
                                exclude={"channel_tentacle_id", "user_id"}
                            ),
                            channel_tentacle_id=channel_id,
                            user_id=owner.id,
                        )
                    )

            await session.commit()

        self.users.clear()
        for user in users_by_username.values():
            self.cache_user(user)
