from __future__ import annotations

import asyncio

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from octomate.database import async_session
from octomate.managers.user import UserManager
from octomate.schemas.user import User, UserProfile
from octomate.types.threads import CLAUDE_NATIVE_ID
from tests.support.users import a_user


@pytest.fixture(autouse=True)
async def _db(in_memory_engine: AsyncEngine) -> None:
    return


async def find_profile(channel: str, channel_user_id: str) -> UserProfile | None:
    async with async_session() as session:
        return await session.one_or_none(
            UserProfile,
            expressions=[
                UserProfile["channel_tentacle_id"] == channel,
                UserProfile["channel_user_id"] == channel_user_id,
            ],
        )


async def test_first_sighting_creates_an_ownerless_visitor() -> None:
    manager = UserManager()

    profile = await manager.ensure_profile(
        "slack", UserProfile(channel_user_id="U1", name="Lu")
    )

    assert profile.channel_tentacle_id == "slack"
    assert profile.channel_user_id == "U1"
    assert profile.name == "Lu"
    assert profile.user_id is None
    assert profile.user.peek() is None
    assert await manager.owner(profile) is None
    async with async_session() as session:
        assert list(await session.list(User, limit=None)) == []


async def test_ensure_profile_serializes_concurrent_first_sightings() -> None:
    manager = UserManager()

    profiles = await asyncio.gather(
        *(
            manager.ensure_profile(
                "slack", UserProfile(channel_user_id="U1", name="Lu")
            )
            for _ in range(3)
        )
    )

    assert len({profile.id for profile in profiles}) == 1
    assert {profile.user_id for profile in profiles} == {None}


@pytest.mark.parametrize(("channel_id", "user_id"), [("slack", "U2"), ("lark", "U1")])
async def test_ensure_locks_only_the_matching_profile(
    channel_id: str, user_id: str
) -> None:
    manager = UserManager()

    async with asyncio.timeout(2), asyncio.TaskGroup() as tasks:
        async with manager.lock(("slack", "U1")):
            duplicate = tasks.create_task(
                manager.ensure_profile("slack", UserProfile(channel_user_id="U1"))
            )
            await asyncio.sleep(0)
            other = await manager.ensure_profile(
                channel_id, UserProfile(channel_user_id=user_id)
            )
            assert not duplicate.done()
        assert (await duplicate).id != other.id


async def test_profile_tracks_the_latest_channel_snapshot() -> None:
    manager = UserManager()
    first = await manager.ensure_profile(
        "lark", UserProfile(channel_user_id="ou_1", name="Original")
    )
    changed = await manager.ensure_profile(
        "lark", UserProfile(channel_user_id="ou_1", name="Changed")
    )
    blanked = await manager.ensure_profile(
        "lark", UserProfile(channel_user_id="ou_1", name="")
    )

    assert changed.id == first.id == blanked.id
    assert blanked.name == ""
    assert blanked.user_id is None


async def test_profile_finds_an_observed_identity_and_misses_an_unknown_one() -> None:
    manager = UserManager()
    observed = await manager.ensure_profile(
        "lark", UserProfile(channel_user_id="ou_1", name="Original")
    )

    found = await manager.profile("lark", "ou_1")
    assert found is not None
    assert found.id == observed.id
    assert await manager.profile("lark", "ou_2") is None
    assert await manager.profile("slack", "ou_1") is None


async def test_owner_loads_a_registered_user_with_a_fresh_manager() -> None:
    await a_user("luhui", profiles={"slack": "U1"})
    profile = await find_profile("slack", "U1")
    assert profile is not None

    owner = await UserManager().owner(profile)

    assert owner is not None
    assert owner.username == "luhui"


async def test_owner_reloads_updates_from_the_database() -> None:
    user = await a_user("luhui", name="Original", profiles={"slack": "U1"})
    async with async_session() as session:
        profile = await session.one_or_none(
            UserProfile, expressions=[UserProfile["user_id"] == user.id]
        )
        assert profile is not None
        related = await profile.user
        assert related is not None
        assert related.name == "Original"
    manager = UserManager()
    first = await manager.owner(profile)
    assert first is not None
    assert first.name == "Original"

    async with async_session() as session:
        stored = await session.get(User, user.id)
        assert stored is not None
        stored.name = "Updated"
        await session.commit()

    fresh = await manager.owner(profile)
    assert fresh is not None
    assert fresh.name == "Updated"


async def test_native_profile_reloads_updates_and_deletion_from_the_database() -> None:
    user = await a_user("original", name="Original")
    manager = UserManager()
    first = await manager.native_profile(CLAUDE_NATIVE_ID, "original")
    assert first is not None

    async with async_session() as session:
        stored = await session.get(User, user.id)
        assert stored is not None
        stored.name = "Updated"
        await session.commit()

    fresh = await manager.native_profile(CLAUDE_NATIVE_ID, "original")
    assert fresh is not None
    assert fresh.user_id == user.id
    assert fresh.name == "Updated"

    async with async_session() as session:
        stored = await session.get(User, user.id)
        assert stored is not None
        await session.delete(stored)
        await session.commit()
    assert await manager.native_profile(CLAUDE_NATIVE_ID, "original") is None


async def test_linked_profiles_follow_a_registered_human(
    in_memory_engine: None,
) -> None:
    """The reverse of `owner`: not whose account this is, but where else they are."""
    await a_user("luhui", name="Kalynnka", profiles={"slack": "U1", "lark": "ou_1"})
    manager = UserManager()
    here = await manager.ensure_profile("slack", UserProfile(channel_user_id="U1"))

    linked = await manager.linked_profiles(here)

    assert [(p.channel_tentacle_id, p.channel_user_id) for p in linked] == [
        ("lark", "ou_1")
    ]

    async with async_session() as session:
        other = await session.get(UserProfile, linked[0].id)
        assert other is not None
        other.user_id = None
        await session.commit()
    assert await manager.linked_profiles(here) == []


async def test_linked_profiles_are_empty_for_a_visitor(
    in_memory_engine: None,
) -> None:
    # An unregistered account is one account: nothing links it anywhere, so a
    # cross-channel move can never follow a stranger.
    manager = UserManager()
    visitor = await manager.ensure_profile("slack", UserProfile(channel_user_id="U9"))

    assert await manager.linked_profiles(visitor) == []


async def test_ensure_profile_keeps_a_verified_owner() -> None:
    """A channel observation never carries `user_id`; the native ingest's
    transient anchor does, and that verified ownership persists — the row is
    created owned, and re-anchored by a later write."""
    await a_user("lu")
    manager = UserManager()
    anchor = await manager.native_profile(CLAUDE_NATIVE_ID, "lu")
    assert anchor is not None

    stored = await manager.ensure_profile(CLAUDE_NATIVE_ID, anchor)

    assert stored.user_id == anchor.user_id
    owner = await manager.owner(stored)
    assert owner is not None
    assert owner.username == "lu"

    rewritten = await manager.ensure_profile(CLAUDE_NATIVE_ID, anchor)
    assert rewritten.user_id == anchor.user_id
