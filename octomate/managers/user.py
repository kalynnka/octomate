from __future__ import annotations

from octomate.database import async_session
from octomate.managers.base import Locks, Manager
from octomate.schemas.user import User, UserProfile

PROFILE_FIELDS = {"name", "nickname", "gender", "age", "title"}


class UserManager(Manager, Locks[tuple[str, str]]):
    """The persisted cross-channel user registry, queried fresh on each lookup."""

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
