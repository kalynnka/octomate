"""Thread locations remain independently selectable as channel choices change."""

from unittest.mock import AsyncMock

import pytest
from pydantic import SecretStr
from uuid_utils.compat import uuid7

from octomate import Octomate
from octomate.config.channels import TrunklineChannelConfig
from octomate.schemas.user import UserProfile
from octomate.tentacles.channel import SubThreadLocation, ThreadLocation
from octomate.tentacles.lark.ink import LarkInk
from octomate.tentacles.slack.ink import SlackInk
from octomate.tentacles.trunkline.base import TrunklineTentacle
from tests.support.channels import FakeChannelTentacle


async def test_location_handles_survive_multiple_choices_and_reordering(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    channel = FakeChannelTentacle("chat")
    profile = UserProfile(channel_tentacle_id="chat", channel_user_id="alice")
    default = SubThreadLocation(key="", label="Private", chat_type="dm", chat_id="")
    room = SubThreadLocation(
        key="room", label="Work", chat_type="group", chat_id="room", shared=True
    )
    discover = AsyncMock(return_value=[default])
    monkeypatch.setattr(channel.ink, "thread_locations", discover)
    [original] = await channel.thread_destinations(profile)
    discover.return_value = [room, default]
    shared, private = await channel.thread_destinations(profile)
    assert private == original
    assert private.handle == "chat"
    assert shared.handle == "chat/room"
    assert shared.address.chat_id == "room"
    assert shared.address.user_id == "alice"
    assert shared.address.shared
    assert not private.address.shared
    assert channel.opened_dms == []
    assert channel.sent == []
    await channel.start_thread(shared.address, "Work here")
    assert channel.sub_threads == [(shared.address, "Work here")]
    discover.return_value = [room]
    assert await channel.thread_destinations(profile) == [shared]
    discover.return_value = []
    assert await channel.thread_destinations(profile) == []
    assert all(call.args == ("alice",) for call in discover.await_args_list)


async def test_unlinked_identity_cannot_discover_locations(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    channel = FakeChannelTentacle("chat")
    discover = AsyncMock()
    monkeypatch.setattr(channel.ink, "thread_locations", discover)
    assert (
        await channel.thread_destinations(
            UserProfile(channel_tentacle_id="other", channel_user_id="alice")
        )
        == []
    )
    discover.assert_not_awaited()


@pytest.mark.parametrize("platform", ["slack", "lark"])
async def test_linked_dm_location_is_discovered_without_opening(platform: str) -> None:
    ink = (
        SlackInk(SecretStr("test"))
        if platform == "slack"
        else LarkInk("test", SecretStr("test"))
    )
    [location] = await ink.thread_locations("alice")
    assert isinstance(location, SubThreadLocation)
    assert location.chat_type == "dm"
    assert location.chat_id == ""
    assert location.key == ""
    assert not location.shared


async def test_trunkline_location_uses_registered_identity_from_any_channel() -> None:
    channel = TrunklineTentacle(
        "web", Octomate(), config=TrunklineChannelConfig(agents=["first"])
    )
    channel.self_profile = await channel.ink.inspect()
    user_id = uuid7()
    profile = UserProfile(
        channel_tentacle_id="discord", channel_user_id="100", user_id=user_id
    )
    [location] = await channel.ink.thread_locations(str(user_id))
    assert isinstance(location, ThreadLocation)
    [destination] = await channel.thread_destinations(profile)
    assert destination.handle == "web"
    assert destination.address.chat_type == "thread"
    assert destination.address.chat_id == str(user_id)
    assert destination.address.user_id == str(user_id)
    opened = await channel.start_thread(destination.address, "Continue")
    assert opened.channel_thread_id
    assert opened.chat_id == str(user_id)
    assert (
        await channel.thread_destinations(
            UserProfile(channel_tentacle_id="discord", channel_user_id="100")
        )
        == []
    )
