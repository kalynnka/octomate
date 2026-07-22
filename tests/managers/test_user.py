from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from octomate.config.users import UserConfig
from octomate.managers.user import UserManager
from octomate.schemas.user import UserProfile


@pytest.fixture(autouse=True)
async def _db(in_memory_engine: AsyncEngine) -> AsyncIterator[None]:
    yield


async def loaded(config: dict[str, UserConfig] | None = None) -> UserManager:
    manager = UserManager(config)
    await manager.load()
    return manager


def test_profile_validates_legacy_sender_blob() -> None:
    """Pre-promotion ThreadMessage.sender blobs — `user_id` is the platform
    string and none of the registry fields exist — still validate: the legacy
    shim claims that key for channel_user_id, never the owner FK."""
    blob = {
        "user_id": "U123",
        "name": "Lu",
        "nickname": None,
        "gender": None,
        "age": None,
        "title": None,
    }
    profile = UserProfile.model_validate(blob)
    assert profile.channel_user_id == "U123"
    assert profile.user_id is None
    assert profile.method is None
    assert profile.channel_tentacle_id == ""


async def test_reconcile_links_declared_accounts() -> None:
    manager = await loaded(
        {"luhui": UserConfig.model_validate({"name": "Lu Hui", "profiles": {"slack": "U1", "napcat": "9"}})}
    )
    await manager.reconcile()

    profile = manager.resolve("slack", "U1")
    assert profile is not None
    assert profile.method == "config"
    assert profile.verified_at is not None
    owner = manager.owner_of(profile)
    assert owner is not None and owner.handle == "luhui"
    assert owner.name == "Lu Hui"

    # A fresh manager sees the persisted registry, not just the cache.
    fresh = await loaded()
    stored = fresh.resolve("napcat", "9")
    assert stored is not None
    stored_owner = fresh.owner_of(stored)
    assert stored_owner is not None and stored_owner.handle == "luhui"


async def test_link_fails_fast_across_users() -> None:
    manager = await loaded(
        {
            "a": UserConfig.model_validate({"profiles": {"slack": "U1"}}),
            "b": UserConfig(),
        }
    )
    await manager.reconcile()

    with pytest.raises(ValueError, match="already linked"):
        await manager.link(manager.handles["b"], "slack", "U1", method="code")


async def test_unlink_keeps_the_row() -> None:
    manager = await loaded({"luhui": UserConfig.model_validate({"profiles": {"slack": "U1"}})})
    await manager.reconcile()

    await manager.unlink("slack", "U1")
    profile = manager.resolve("slack", "U1")
    assert profile is not None
    assert profile.user_id is None
    assert profile.method is None
    assert profile.verified_at is None

    fresh = await loaded()
    stored = fresh.resolve("slack", "U1")
    assert stored is not None and stored.user_id is None


async def test_reconcile_is_idempotent() -> None:
    config = {"luhui": UserConfig.model_validate({"name": "Lu", "profiles": {"slack": "U1"}})}
    manager = await loaded(config)
    await manager.reconcile()
    first = manager.resolve("slack", "U1")
    assert first is not None
    verified_at = first.verified_at

    await manager.reconcile()
    second = manager.resolve("slack", "U1")
    assert second is not None and second.verified_at == verified_at

    fresh = await loaded(config)
    await fresh.reconcile()
    assert len(fresh.profiles) == 1
    assert len(fresh.users) == 1


async def test_reconcile_removes_user_and_unlinks_everything() -> None:
    manager = await loaded({"luhui": UserConfig.model_validate({"profiles": {"slack": "U1"}})})
    await manager.reconcile()
    # A handshake link belonging to the same user dies with it: no user, no link.
    await manager.link(manager.handles["luhui"], "napcat", "9", method="code")

    emptied = await loaded()
    await emptied.reconcile()
    assert emptied.handles == {}
    for key in (("slack", "U1"), ("napcat", "9")):
        profile = emptied.resolve(*key)
        assert profile is not None, key
        assert profile.user_id is None


async def test_reconcile_moves_account_between_users() -> None:
    manager = await loaded({"a": UserConfig.model_validate({"profiles": {"slack": "U1"}})})
    await manager.reconcile()

    moved = await loaded({"a": UserConfig(), "b": UserConfig.model_validate({"profiles": {"slack": "U1"}})})
    await moved.reconcile()
    profile = moved.resolve("slack", "U1")
    assert profile is not None
    owner = moved.owner_of(profile)
    assert owner is not None and owner.handle == "b"
    assert profile.method == "config"


async def test_reconcile_preserves_code_links_of_surviving_users() -> None:
    manager = await loaded({"luhui": UserConfig.model_validate({"profiles": {"slack": "U1"}})})
    await manager.reconcile()
    await manager.link(manager.handles["luhui"], "napcat", "9", method="code")

    again = await loaded({"luhui": UserConfig.model_validate({"profiles": {"slack": "U1"}})})
    await again.reconcile()
    code_link = again.resolve("napcat", "9")
    assert code_link is not None
    assert code_link.method == "code"
    code_owner = again.owner_of(code_link)
    assert code_owner is not None and code_owner.handle == "luhui"


async def test_reconcile_requires_load() -> None:
    manager = UserManager()
    with pytest.raises(RuntimeError, match="load"):
        await manager.reconcile()


async def test_config_profile_seeds_but_never_overwrites_observations() -> None:
    manager = await loaded(
        {
            "luhui": UserConfig(
                profiles={
                    "lark": UserProfile(channel_user_id="ou_1", name="Lu on Lark"),
                }
            )
        }
    )
    await manager.reconcile()
    seeded = manager.resolve("lark", "ou_1")
    assert seeded is not None and seeded.name == "Lu on Lark"

    # An ingest observation later updates the row; the next reconcile with the
    # same config must not overwrite what was observed.
    await manager.ensure_profile(
        "lark", UserProfile(channel_user_id="ou_1", name="观察到的名字")
    )
    await manager.reconcile()
    observed = manager.resolve("lark", "ou_1")
    assert observed is not None and observed.name == "观察到的名字"
