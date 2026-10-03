"""Discovery suggests addresses; channel preparation validates them independently."""

from unittest.mock import AsyncMock

import pytest
from pydantic import SecretStr, TypeAdapter
from uuid_utils.compat import uuid7

from octomate import Octomate
from octomate.config.channels import TrunklineChannelConfig
from octomate.schemas.conversation import ChannelAddress
from octomate.schemas.user import UserProfile
from octomate.tentacles.discord.schema import DiscordAddress
from octomate.tentacles.lark.ink import LarkInk
from octomate.tentacles.slack.ink import SlackInk
from octomate.tentacles.trunkline.base import TrunklineTentacle
from tests.channels.lark.fakes import FakeLarkInk, lark_channel
from tests.channels.slack.fakes import FakeSlackInk, slack_channel
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


async def test_listing_uses_the_linked_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    channel = FakeChannelTentacle("chat")
    profile = UserProfile(channel_tentacle_id="chat", channel_user_id="alice")
    with pytest.raises(ValueError, match="cannot be browsed"):
        await channel.list_addresses(profile)
    listing = AsyncMock(return_value=[])
    monkeypatch.setattr(channel.ink, "list_addresses", listing)
    with pytest.raises(ValueError, match="not linked"):
        await channel.list_addresses(
            UserProfile(channel_tentacle_id="other", channel_user_id="alice")
        )
    listing.assert_not_awaited()
    await channel.list_addresses(profile, "200")
    listing.assert_awaited_once_with(ChannelAddress("chat", "dm", "", "alice"), "200")


def test_metadata_and_a_channel_subclass_leave_the_address_the_same() -> None:
    plain = ChannelAddress("discord", "group", "400", "100", shared=True)
    named = DiscordAddress(
        "discord",
        "group",
        "400",
        "100",
        shared=True,
        metadata={"name": "planning", "server": "Development"},
    )
    assert named == plain
    assert plain == named
    assert {plain, named} == {plain}
    assert named != ChannelAddress("discord", "group", "401", "100", shared=True)
    stored = TypeAdapter(ChannelAddress).dump_json(named)
    assert TypeAdapter(ChannelAddress).validate_json(stored).metadata == named.metadata
    # An address written before the field existed still reads.
    legacy = '{"channel_tentacle_id":"discord","chat_type":"dm","chat_id":"","user_id":"100"}'
    assert TypeAdapter(ChannelAddress).validate_json(legacy).metadata == {}


def test_a_channel_tells_who_can_read_a_thread_from_its_address() -> None:
    slack = slack_channel(FakeSlackInk())
    lark = lark_channel(FakeLarkInk())
    web = TrunklineTentacle(
        "web", Octomate(), config=TrunklineChannelConfig(agents=["first"])
    )
    plain = FakeChannelTentacle("chat")

    # A thread keeps the id of the chat it sits in, and a direct chat's id says so.
    assert not slack.is_shared(ChannelAddress("slack", "thread", "D1", "U1", "17.1"))
    assert slack.is_shared(ChannelAddress("slack", "thread", "C1", "U1", "17.1"))
    assert not lark.is_shared(
        ChannelAddress("lark", "thread", "ou_alice", "ou_alice", "om_1")
    )
    assert lark.is_shared(
        ChannelAddress("lark", "thread", "oc_room", "ou_alice", "om_1")
    )
    assert not web.is_shared(ChannelAddress("web", "thread", "owner", "owner", "t1"))
    # A channel with nothing to read it from takes a thread for shared.
    assert plain.is_shared(ChannelAddress("chat", "thread", "room", "alice", "t1"))
    assert not plain.is_shared(ChannelAddress("chat", "dm", "alice", "alice"))
