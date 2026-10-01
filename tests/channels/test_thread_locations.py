"""Discovery suggests addresses; channel preparation validates them independently."""

from unittest.mock import AsyncMock

import pytest
from pydantic import SecretStr
from uuid_utils.compat import uuid7

from octomate import Octomate
from octomate.config.channels import TrunklineChannelConfig
from octomate.schemas.conversation import ChannelAddress
from octomate.schemas.user import UserProfile
from octomate.tentacles.lark.ink import LarkInk
from octomate.tentacles.slack.ink import SlackInk
from octomate.tentacles.trunkline.base import TrunklineTentacle
from tests.support.channels import FakeChannelTentacle


async def test_addresses_survive_multiple_choices_and_reordering(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    channel = FakeChannelTentacle("chat")
    profile = UserProfile(channel_tentacle_id="chat", channel_user_id="alice")
    private = ChannelAddress("chat", "dm", "", "alice")
    shared = ChannelAddress("chat", "group", "room", "alice", shared=True)
    discover = AsyncMock(return_value=[private])
    monkeypatch.setattr(channel.ink, "suggest_addresses", discover)
    assert await channel.suggest_addresses(profile) == [private]
    discover.return_value = [shared, private]
    assert await channel.suggest_addresses(profile) == [shared, private]
    discover.return_value = []
    assert await channel.prepare_address(shared) == shared
    assert channel.opened_dms == []
    assert channel.sent == []
    await channel.start_thread(shared, "Work here")
    assert channel.sub_threads == [(shared, "Work here")]
    assert await channel.suggest_addresses(profile) == []
    assert all(call.args == (private, None) for call in discover.await_args_list)


async def test_unlinked_identity_cannot_discover_addresses(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    channel = FakeChannelTentacle("chat")
    discover = AsyncMock()
    monkeypatch.setattr(channel.ink, "suggest_addresses", discover)
    assert (
        await channel.suggest_addresses(
            UserProfile(channel_tentacle_id="other", channel_user_id="alice")
        )
        == []
    )
    discover.assert_not_awaited()


@pytest.mark.parametrize("platform", ["slack", "lark"])
async def test_linked_dm_is_discovered_without_opening(platform: str) -> None:
    ink = (
        SlackInk(SecretStr("test"))
        if platform == "slack"
        else LarkInk("test", SecretStr("test"))
    )
    address = ChannelAddress(platform, "dm", "", "alice")
    assert await ink.suggest_addresses(address) == [address]
    assert await ink.prepare_address(address) == address
    with pytest.raises(ValueError, match="current chat"):
        await ink.prepare_address(ChannelAddress(platform, "group", "unknown", "alice"))


async def test_trunkline_address_uses_registered_identity_from_any_channel() -> None:
    channel = TrunklineTentacle(
        "web", Octomate(), config=TrunklineChannelConfig(agents=["first"])
    )
    channel.self_profile = await channel.ink.inspect()
    user_id = uuid7()
    profile = UserProfile(
        channel_tentacle_id="discord", channel_user_id="100", user_id=user_id
    )
    [address] = await channel.suggest_addresses(profile)
    assert address == ChannelAddress("web", "thread", str(user_id), str(user_id))
    assert await channel.prepare_address(address) == address
    opened = await channel.start_thread(address, "Continue")
    assert opened.channel_thread_id
    assert opened.chat_id == str(user_id)
    assert (
        await channel.suggest_addresses(
            UserProfile(channel_tentacle_id="discord", channel_user_id="100")
        )
        == []
    )
    with pytest.raises(ValueError, match="another thread"):
        await channel.prepare_address(opened)


@pytest.mark.parametrize("platform", ["slack", "lark"])
@pytest.mark.parametrize("surface", ["dm", "group", "thread"])
async def test_addresses_respect_the_current_parent(
    platform: str, surface: str
) -> None:
    ink = (
        SlackInk(SecretStr("test"))
        if platform == "slack"
        else LarkInk("test", SecretStr("test"))
    )
    source = ChannelAddress(
        platform,
        "dm" if surface == "dm" else "group",
        "current-chat",
        "alice",
        channel_thread_id="existing-thread" if surface == "thread" else None,
        shared=surface != "dm",
    )
    addresses = await ink.suggest_addresses(
        ChannelAddress(platform, "dm", "", "alice"), source
    )
    assert addresses == ([] if surface == "thread" else [source])
    if surface != "thread":
        assert await ink.prepare_address(source, source) == source


async def test_foreign_channel_context_does_not_leak_into_discovery(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    channel = FakeChannelTentacle("chat")
    discover = AsyncMock(return_value=[])
    monkeypatch.setattr(channel.ink, "suggest_addresses", discover)
    await channel.suggest_addresses(
        UserProfile(channel_tentacle_id="chat", channel_user_id="alice"),
        ChannelAddress("elsewhere", "group", "secret", "other", shared=True),
    )
    discover.assert_awaited_once_with(ChannelAddress("chat", "dm", "", "alice"), None)
