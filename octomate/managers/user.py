from __future__ import annotations

import asyncio
import uuid

from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from octomate.config.users import UserConfig
from octomate.database import async_session
from octomate.schemas.user import User, UserProfile

PROFILE_FIELDS = {"name", "nickname", "gender", "age", "title"}


class UserManager:
    """The cross-channel user registry, currently configured through YAML.

    A persisted profile is an observed channel identity. It belongs to a
    registered ``User`` only when the ``users:`` YAML config declares that
    exact channel account; every other profile is a visitor with no owner.

    Profiles are queried by their indexed channel identity. Users are expected
    to remain a small registry and are all cached for profile-to-owner lookup.
    """

    def __init__(self, config: dict[str, UserConfig] | None = None) -> None:
        self.config = config or {}
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

    async def ensure_profile(
        self, channel_tentacle_id: str, observed: UserProfile
    ) -> UserProfile:
        """Persist the latest channel snapshot and return its registry profile.

        First sight creates an ownerless visitor profile. YAML reconciliation
        may already have seeded and attached the profile; observations refresh
        only its channel-owned display fields and never change ownership.
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
                    **observed.model_dump(include=PROFILE_FIELDS),
                )
                session.add(profile)
            else:
                for name in PROFILE_FIELDS:
                    setattr(profile, name, getattr(observed, name))

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
        overwrite an observed row.
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
                user = users_by_username.get(username)
                if user is None:
                    user = User(
                        username=username,
                        name=name,
                        nickname=user_config.nickname,
                    )
                    session.add(user)
                    users_by_username[username] = user
                else:
                    user.name = name
                    user.nickname = user_config.nickname

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
                username = declared.get(key)
                if username is None:
                    profile.user_id = None
                    profile.user.value = None
                    continue
                owner = users_by_username[username]
                profile.user_id = owner.id
                profile.user.value = owner

            profile_upserts = []
            for username, user_config in self.config.items():
                owner = users_by_username[username]
                for channel_id, config_profile in user_config.profiles.items():
                    key = (channel_id, config_profile.channel_user_id)
                    if key in profiles_by_key:
                        continue
                    profile_upserts.append(
                        {
                            **config_profile.model_dump(),
                            "channel_tentacle_id": channel_id,
                            "user_id": owner.id,
                        }
                    )

            if profile_upserts:
                profile_insert = sqlite_insert(UserProfile).values(profile_upserts)
                await session.execute(
                    profile_insert.on_conflict_do_update(
                        index_elements=[
                            "channel_tentacle_id",
                            "channel_user_id",
                        ],
                        set_={"user_id": profile_insert.excluded.user_id},
                    )
                )

            await session.commit()

        self.users.clear()
        for user in users_by_username.values():
            self.cache_user(user)
