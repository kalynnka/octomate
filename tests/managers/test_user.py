from __future__ import annotations

import asyncio
from collections.abc import Mapping

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from octomate.config.users import UserConfig
from octomate.database import async_session
from octomate.managers.user import UserManager
from octomate.schemas.user import User, UserProfile


@pytest.fixture(autouse=True)
async def _db(in_memory_engine: AsyncEngine) -> None:
    return


def config(spec: Mapping[str, object]) -> dict[str, UserConfig]:
    return {
        username: UserConfig.model_validate(value) for username, value in spec.items()
    }


def profile_config(channel_user_id: str) -> dict[str, str]:
    return {"channel_user_id": channel_user_id}


async def find_profile(channel: str, channel_user_id: str) -> UserProfile | None:
    async with async_session() as session:
        return await session.one_or_none(
            UserProfile,
            expressions=[
                UserProfile["channel_tentacle_id"] == channel,
                UserProfile["channel_user_id"] == channel_user_id,
            ],
        )


async def find_user(username: str) -> User | None:
    async with async_session() as session:
        return await session.one_or_none(
            User,
            expressions=[User["username"] == username],
        )


async def owner_of(manager: UserManager, channel: str, channel_user_id: str) -> User:
    profile = await find_profile(channel, channel_user_id)
    assert profile is not None
    owner = await manager.owner(profile)
    assert owner is not None
    return owner


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


async def test_reconcile_groups_declared_profiles_under_one_yaml_user() -> None:
    manager = UserManager(
        config(
            {
                "luhui": {
                    "name": "Lu Hui",
                    "profiles": {
                        "slack": profile_config("U1"),
                        "napcat": profile_config("9"),
                    },
                }
            }
        )
    )

    await manager.reconcile()

    slack = await find_profile("slack", "U1")
    napcat = await find_profile("napcat", "9")
    assert slack is not None
    assert napcat is not None
    assert slack.user_id == napcat.user_id
    owner = await manager.owner(slack)
    assert owner is not None
    assert owner.username == "luhui"
    assert owner.name == "Lu Hui"


async def test_yaml_key_is_the_stable_username_and_default_name() -> None:
    manager = UserManager(
        config(
            {
                "luhui": {
                    "nickname": "Lu",
                    "profiles": {"slack": profile_config("U1")},
                }
            }
        )
    )

    await manager.reconcile()

    owner = await owner_of(manager, "slack", "U1")
    assert owner.username == "luhui"
    assert owner.name == "luhui"
    assert owner.nickname == "Lu"


async def test_reconcile_rejects_a_profile_declared_for_two_users() -> None:
    manager = UserManager(
        config(
            {
                "a": {"profiles": {"slack": profile_config("U1")}},
                "b": {"profiles": {"slack": profile_config("U1")}},
            }
        )
    )

    with pytest.raises(ValueError, match="declared for both"):
        await manager.reconcile()

    assert await find_profile("slack", "U1") is None
    assert await find_user("a") is None
    assert await find_user("b") is None


async def test_reconcile_is_idempotent_and_renames_by_stable_username() -> None:
    manager = UserManager(
        config(
            {
                "stable-key": {
                    "name": "Old Name",
                    "profiles": {"slack": profile_config("U1")},
                }
            }
        )
    )
    await manager.reconcile()
    original = await owner_of(manager, "slack", "U1")

    await manager.reconcile()
    renamed = UserManager(
        config(
            {
                "stable-key": {
                    "name": "New Name",
                    "profiles": {"slack": profile_config("U1")},
                }
            }
        )
    )
    await renamed.reconcile()

    current = await owner_of(renamed, "slack", "U1")
    assert current.id == original.id
    assert current.username == "stable-key"
    assert current.name == "New Name"


async def test_reconcile_removing_a_user_retains_it_and_unlinks_profiles() -> None:
    manager = UserManager(
        config(
            {
                "luhui": {
                    "profiles": {
                        "slack": profile_config("U1"),
                        "lark": profile_config("ou_1"),
                    },
                }
            }
        )
    )
    await manager.reconcile()
    removed_user_id = (await owner_of(manager, "slack", "U1")).id

    empty_config_manager = UserManager()
    await empty_config_manager.reconcile()

    slack = await find_profile("slack", "U1")
    lark = await find_profile("lark", "ou_1")
    assert slack is not None
    assert slack.user_id is None
    assert lark is not None
    assert lark.user_id is None
    async with async_session() as session:
        retained = await session.get(User, removed_user_id)
    assert retained is not None
    assert set(empty_config_manager.users) == {removed_user_id}
    assert empty_config_manager.users[removed_user_id].username == retained.username


async def test_reconcile_removing_one_profile_makes_only_it_a_visitor() -> None:
    original = UserManager(
        config(
            {
                "luhui": {
                    "profiles": {
                        "slack": profile_config("U1"),
                        "lark": profile_config("ou_1"),
                    },
                }
            }
        )
    )
    await original.reconcile()
    user_id = (await owner_of(original, "slack", "U1")).id

    changed = UserManager(
        config({"luhui": {"profiles": {"slack": profile_config("U1")}}})
    )
    await changed.reconcile()

    slack = await find_profile("slack", "U1")
    lark = await find_profile("lark", "ou_1")
    assert slack is not None
    assert slack.user_id == user_id
    assert lark is not None
    assert lark.user_id is None


async def test_reconcile_moves_a_profile_between_yaml_users() -> None:
    await UserManager(
        config(
            {
                "a": {"profiles": {"slack": profile_config("U1")}},
                "b": {"profiles": {"lark": profile_config("ou_1")}},
            }
        )
    ).reconcile()

    moved = UserManager(
        config(
            {
                "a": {"profiles": {}},
                "b": {
                    "profiles": {
                        "slack": profile_config("U1"),
                        "lark": profile_config("ou_1"),
                    }
                },
            }
        )
    )
    await moved.reconcile()

    slack_owner = await owner_of(moved, "slack", "U1")
    lark_owner = await owner_of(moved, "lark", "ou_1")
    assert slack_owner.id == lark_owner.id
    assert slack_owner.username == "b"


async def test_reconcile_claims_an_observed_visitor() -> None:
    visitor_manager = UserManager()
    visitor = await visitor_manager.ensure_profile(
        "slack", UserProfile(channel_user_id="U1", name="Observed Name")
    )
    assert visitor.user_id is None

    manager = UserManager(
        config(
            {
                "luhui": {
                    "name": "Lu Hui",
                    "profiles": {
                        "slack": {
                            "channel_user_id": "U1",
                            "name": "Configured Name",
                        }
                    },
                }
            }
        )
    )
    await manager.reconcile()

    claimed = await find_profile("slack", "U1")
    assert claimed is not None
    assert claimed.id == visitor.id
    assert claimed.name == "Observed Name"
    assert (await manager.owner(claimed)) is not None


async def test_config_seeds_a_never_seen_profile() -> None:
    manager = UserManager(
        config(
            {
                "luhui": {
                    "name": "Lu Hui",
                    "profiles": {
                        "lark": {
                            "channel_user_id": "ou_1",
                            "name": "Lu on Lark",
                        }
                    },
                }
            }
        )
    )

    await manager.reconcile()

    seeded = await find_profile("lark", "ou_1")
    assert seeded is not None
    assert seeded.name == "Lu on Lark"
    owner = await manager.owner(seeded)
    assert owner is not None
    assert owner.username == "luhui"


async def test_yaml_can_declare_a_registered_user_without_profiles() -> None:
    manager = UserManager(config({"luhui": {"name": "Lu Hui"}}))

    await manager.reconcile()

    user = await find_user("luhui")
    assert user is not None
    assert user.name == "Lu Hui"
    assert list(await user.profiles) == []


async def test_owner_loads_a_registered_user_with_a_fresh_manager() -> None:
    await UserManager(
        config({"luhui": {"profiles": {"slack": profile_config("U1")}}})
    ).reconcile()
    profile = await find_profile("slack", "U1")
    assert profile is not None

    owner = await UserManager().owner(profile)

    assert owner is not None
    assert owner.username == "luhui"


async def test_linked_profiles_follow_a_registered_human(
    in_memory_engine: None,
) -> None:
    """The reverse of `owner`: not whose account this is, but where else they are."""
    manager = UserManager(
        {
            "luhui": UserConfig.model_validate(
                {
                    "name": "Kalynnka",
                    "profiles": {
                        "slack": {"channel_user_id": "U1"},
                        "lark": {"channel_user_id": "ou_1"},
                    },
                }
            )
        }
    )
    await manager.reconcile()
    here = await manager.ensure_profile("slack", UserProfile(channel_user_id="U1"))

    linked = await manager.linked_profiles(here)

    assert [(p.channel_tentacle_id, p.channel_user_id) for p in linked] == [
        ("lark", "ou_1")
    ]


async def test_linked_profiles_are_empty_for_a_visitor(
    in_memory_engine: None,
) -> None:
    # An unregistered account is one account: nothing links it anywhere, so a
    # cross-channel move can never follow a stranger.
    manager = UserManager()
    visitor = await manager.ensure_profile("slack", UserProfile(channel_user_id="U9"))

    assert await manager.linked_profiles(visitor) == []
