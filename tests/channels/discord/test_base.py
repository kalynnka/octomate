from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Coroutine
from dataclasses import dataclass, replace
from typing import ClassVar
from unittest.mock import AsyncMock, Mock

import discord
import pytest
from pydantic import SecretStr

from octomate import Octomate
from octomate.config import DiscordChannelConfig
from octomate.schemas.conversation import ChannelAddress
from octomate.schemas.user import UserProfile
from octomate.tentacles.channel import ChannelSurfaces, build_channel
from octomate.tentacles.discord import (
    DiscordChromo,
    DiscordInk,
    DiscordTentacle,
)
from octomate.tentacles.discord.feelers.approvals import DiscordApprovalFeeler
from octomate.tentacles.discord.feelers.oauth import DiscordOAuthFeeler
from octomate.tentacles.discord.feelers.output import DiscordTimelineFeeler
from octomate.tentacles.discord.feelers.questions import DiscordAskQuestionFeeler
from tests.channels.discord.fakes import (
    a_client_user,
    a_dm_channel,
    a_message,
    a_text_channel,
    a_thread,
    a_user,
)

DiscordMessageListener = Callable[[discord.Message], Coroutine[None, None, None]]


@dataclass
class FakeDiscordUser:
    id: int = 42
    display_name: str = "Octomate"


class FakeDiscordClient:
    instances: ClassVar[list[FakeDiscordClient]] = []

    def __init__(self, *, intents: discord.Intents) -> None:
        self.intents = intents
        self.user: FakeDiscordUser | None = None
        self.login_token: str | None = None
        self.connect_reconnect: bool | None = None
        self.connect_error: Exception | None = None
        self.become_ready = True
        self.ready = asyncio.Event()
        self.closed = asyncio.Event()
        self.lifecycle: list[str] = []
        self.listeners: dict[str, DiscordMessageListener] = {}
        self.dynamic_items: list[str] = []
        self.instances.append(self)

    def event(self, listener: DiscordMessageListener) -> DiscordMessageListener:
        self.lifecycle.append(f"event:{listener.__name__}")
        self.listeners[listener.__name__] = listener
        return listener

    def add_dynamic_items(
        self,
        *items: type[discord.ui.DynamicItem[discord.ui.Item[discord.ui.View]]],
    ) -> None:
        self.dynamic_items.extend(item.__name__ for item in items)

    async def login(self, token: str) -> None:
        self.lifecycle.append("login")
        self.login_token = token
        self.user = FakeDiscordUser()

    async def connect(self, *, reconnect: bool) -> None:
        self.lifecycle.append("connect")
        self.connect_reconnect = reconnect
        if self.connect_error is not None:
            raise self.connect_error
        if self.become_ready:
            self.ready.set()
        await self.closed.wait()

    async def wait_until_ready(self) -> None:
        await self.ready.wait()

    async def close(self) -> None:
        self.closed.set()


@pytest.fixture
def config() -> DiscordChannelConfig:
    return DiscordChannelConfig(
        bot_token=SecretStr("discord-test"),
        agents=["inkling"],
    )


def test_build_channel_composes_discord_components(
    config: DiscordChannelConfig,
) -> None:
    channel = build_channel("discord-main", config, Octomate())

    assert isinstance(channel, DiscordTentacle)
    assert isinstance(channel.ink, DiscordInk)
    assert isinstance(channel.chromo, DiscordChromo)
    assert isinstance(channel.feelers.timeline, DiscordTimelineFeeler)
    assert isinstance(channel.feelers.approvals, DiscordApprovalFeeler)
    assert isinstance(channel.feelers.ask_questions, DiscordAskQuestionFeeler)
    assert isinstance(channel.feelers.oauth, DiscordOAuthFeeler)
    assert channel.client.intents.message_content is True
    assert DiscordTentacle.thread_strategy == "flat_thread"
    assert DiscordTentacle.surfaces == ChannelSurfaces(
        sub_thread=True,
        direct_message=True,
    )


def test_discord_logs_use_brand_color_after_connection(
    config: DiscordChannelConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    octomate = Octomate()
    monkeypatch.setattr(discord, "Client", FakeDiscordClient)
    channel = DiscordTentacle("discord-main", octomate, config=config)
    octomate.connect(channel)

    assert octomate.log_tag("discord.client") == (
        "discord-main",
        DiscordTentacle.brand_color,
    )


async def test_gateway_lifecycle(
    config: DiscordChannelConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    FakeDiscordClient.instances.clear()
    monkeypatch.setattr(discord, "Client", FakeDiscordClient)
    channel = DiscordTentacle("discord-main", Octomate(), config=config)
    client = FakeDiscordClient.instances[0]

    assert client.dynamic_items == [
        "DiscordApprovalButton",
        "DiscordQuestionAnswerButton",
        "DiscordQuestionChoiceButton",
        "DiscordQuestionNavButton",
    ]

    async with channel:
        assert client.lifecycle[:2] == ["event:on_message", "login"]
        assert client.listeners["on_message"] == channel.on_message
        assert client.login_token == "discord-test"
        assert client.connect_reconnect is True
        assert channel.name == "Octomate"
        assert channel.gateway_task is not None

    assert client.closed.is_set()
    assert channel.gateway_task is None


async def test_gateway_start_failure_closes_all_resources(
    config: DiscordChannelConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    FakeDiscordClient.instances.clear()
    monkeypatch.setattr(discord, "Client", FakeDiscordClient)
    channel = DiscordTentacle("discord-main", Octomate(), config=config)
    client = FakeDiscordClient.instances[0]
    client.connect_error = RuntimeError("Gateway rejected the connection")

    with pytest.raises(RuntimeError, match="Gateway rejected"):
        await channel.__aenter__()

    assert client.closed.is_set()
    assert channel.gateway_task is None
    assert channel.ink.http.is_closed


async def test_cancelled_gateway_start_closes_the_detached_task(
    config: DiscordChannelConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    FakeDiscordClient.instances.clear()
    monkeypatch.setattr(discord, "Client", FakeDiscordClient)
    channel = DiscordTentacle("discord-main", Octomate(), config=config)
    client = FakeDiscordClient.instances[0]
    client.become_ready = False

    with pytest.raises(TimeoutError):
        await asyncio.wait_for(channel.__aenter__(), timeout=0.01)

    assert client.closed.is_set()
    assert channel.gateway_task is None
    assert channel.ink.http.is_closed


async def test_message_listener_tracks_ingest_without_blocking(
    config: DiscordChannelConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    channel = DiscordTentacle("discord-main", Octomate(), config=config)
    message = a_message(a_dm_channel(), message_id=700)
    started = asyncio.Event()
    release = asyncio.Event()

    async def slow_ingest(raw: discord.Message) -> None:
        assert raw is message
        started.set()
        await release.wait()

    monkeypatch.setattr(channel, "ingest", slow_ingest)

    await channel.on_message(message)

    assert len(channel.ingest_tasks) == 1
    await asyncio.sleep(0)
    assert started.is_set()
    task = next(iter(channel.ingest_tasks))
    assert task.get_name() == "discord:discord-main:message:700"

    release.set()
    await task
    await asyncio.sleep(0)
    assert channel.ingest_tasks == set()


async def test_gateway_shutdown_cancels_inflight_ingest(
    config: DiscordChannelConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    FakeDiscordClient.instances.clear()
    monkeypatch.setattr(discord, "Client", FakeDiscordClient)
    channel = DiscordTentacle("discord-main", Octomate(), config=config)
    started = asyncio.Event()
    cancelled = asyncio.Event()

    async def ingest_until_cancelled(raw: discord.Message) -> None:
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    monkeypatch.setattr(channel, "ingest", ingest_until_cancelled)

    async with channel:
        await channel.on_message(a_message(a_dm_channel()))
        await started.wait()
        assert channel.ingest_tasks

    assert cancelled.is_set()
    assert channel.ingest_tasks == set()


async def test_message_listener_ignores_non_human_messages(
    config: DiscordChannelConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    channel = DiscordTentacle("discord-main", Octomate(), config=config)
    channel.client._connection.user = a_client_user()
    target = a_text_channel()
    ingested: list[discord.Message] = []

    async def record_ingest(message: discord.Message) -> None:
        ingested.append(message)

    monkeypatch.setattr(channel, "ingest", record_ingest)

    await channel.on_message(a_message(target, author=a_user(bot=True)))
    await channel.on_message(a_message(target, author=a_user(42)))
    await channel.on_message(a_message(target, webhook_id=600))
    await channel.on_message(
        a_message(target, message_type=discord.MessageType.recipient_add)
    )
    await asyncio.sleep(0)

    assert ingested == []
    assert channel.ingest_tasks == set()


async def test_message_listener_logs_an_escaped_ingest_failure(
    config: DiscordChannelConfig,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    channel = DiscordTentacle("discord-main", Octomate(), config=config)

    async def fail_ingest(message: discord.Message) -> None:
        raise RuntimeError(f"failed on {message.id}")

    monkeypatch.setattr(channel, "ingest", fail_ingest)

    with caplog.at_level(logging.ERROR):
        await channel.on_message(a_message(a_dm_channel(), message_id=700))
        await asyncio.sleep(0)
        await asyncio.sleep(0)

    assert channel.ingest_tasks == set()
    assert "failed to handle Discord message" in caplog.text
    assert "failed on 700" in caplog.text


@pytest.mark.parametrize(
    ("cached", "shared"), [("private", False), ("public", True), (None, True)]
)
def test_a_private_thread_is_read_by_its_members_only(
    config: DiscordChannelConfig,
    monkeypatch: pytest.MonkeyPatch,
    cached: str | None,
    shared: bool,
) -> None:
    channel = DiscordTentacle("discord-main", Octomate(), config=config)
    thread = a_thread(501, private=cached == "private") if cached is not None else None
    monkeypatch.setattr(channel.ink.client, "get_channel", lambda channel_id: thread)

    assert (
        channel.is_shared(
            ChannelAddress(channel.id, "thread", "400", "100", channel_thread_id="501")
        )
        is shared
    )
    assert not channel.is_shared(ChannelAddress(channel.id, "dm", "300", "100"))


async def test_start_sub_thread_creates_a_public_thread_for_a_group(
    config: DiscordChannelConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    channel = DiscordTentacle("discord-main", Octomate(), config=config)
    calls: list[tuple[str, str, str | None]] = []

    async def open_thread(
        chat_id: str,
        hint_text: str,
        *,
        user_id: str | None = None,
        private: bool = False,
    ) -> str:
        calls.append((chat_id, hint_text, user_id))
        assert private is False
        return "500"

    monkeypatch.setattr(channel.ink, "open_thread", open_thread)
    address = ChannelAddress(
        channel_tentacle_id=channel.id,
        chat_type="group",
        chat_id="400",
        user_id="100",
        shared=True,
    )

    result = await channel.start_sub_thread(address, "Continue in a thread")

    assert calls == [("400", "Continue in a thread", "100")]
    assert result == ChannelAddress(
        channel_tentacle_id=channel.id,
        chat_type="thread",
        chat_id="400",
        user_id="100",
        channel_thread_id="500",
        shared=True,
    )


async def test_start_sub_thread_uses_base_fallback_for_dm_and_thread(
    config: DiscordChannelConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    channel = DiscordTentacle("discord-main", Octomate(), config=config)
    presented: list[tuple[ChannelAddress, str]] = []

    async def present(address: ChannelAddress, text: str) -> None:
        presented.append((address, text))

    async def reject_public_thread(chat_id: str, hint_text: str) -> str:
        raise AssertionError((chat_id, hint_text))

    monkeypatch.setattr(channel.feelers.markdown, "present", present)
    monkeypatch.setattr(channel.ink, "open_thread", reject_public_thread)
    addresses = [
        ChannelAddress(
            channel_tentacle_id=channel.id,
            chat_type="dm",
            chat_id=str(a_dm_channel().id),
            user_id="100",
        ),
        ChannelAddress(
            channel_tentacle_id=channel.id,
            chat_type="thread",
            chat_id="400",
            user_id="100",
            channel_thread_id=str(a_thread().id),
            shared=True,
        ),
    ]

    returned = [
        await channel.start_sub_thread(address, "Stay on this surface")
        for address in addresses
    ]

    assert returned == addresses
    assert presented == [(address, "Stay on this surface") for address in addresses]


@pytest.mark.parametrize("denied", [None, "user", "bot", "missing"])
async def test_gateway_destinations_check_discord_membership_and_permissions(
    monkeypatch: pytest.MonkeyPatch, denied: str | None
) -> None:
    tentacle = DiscordTentacle(
        "discord",
        Octomate(),
        config=DiscordChannelConfig(bot_token=SecretStr("test"), agents=["first"]),
    )
    tentacle.self_profile = UserProfile(channel_user_id="42", name="Discord")
    guild = a_text_channel().guild
    guild.name = "Development"
    channel = a_text_channel(guild=guild)
    channel.name = "work"
    channel.position = 0
    other = a_text_channel(401, guild=guild)
    other.name = "planning"
    other.position = 1
    guild._channels = {channel.id: channel, other.id: other}
    member = Mock(spec=discord.Member)
    bot = Mock(spec=discord.Member)

    def guilds(self: discord.Client) -> list[discord.Guild]:
        raise AssertionError("Discovery must not enumerate every server")

    monkeypatch.setattr(discord.Client, "guilds", property(guilds))
    monkeypatch.setattr(discord.Guild, "me", property(lambda self: bot))
    monkeypatch.setattr(discord.Guild, "get_member", lambda self, user_id: member)
    fetch_member = AsyncMock(return_value=member)
    monkeypatch.setattr(discord.Guild, "fetch_member", fetch_member)
    if denied == "missing":
        monkeypatch.setattr(discord.Guild, "get_member", lambda self, user_id: None)
        fetch_member.side_effect = discord.NotFound(
            Mock(status=404, reason="Not Found"), "Unknown Member"
        )
    allowed = discord.Permissions(
        view_channel=True,
        send_messages=True,
        create_public_threads=True,
        send_messages_in_threads=True,
    )
    blocked = discord.Permissions.none()

    def permissions(
        self: discord.TextChannel, who: discord.Member
    ) -> discord.Permissions:
        return (
            blocked
            if (denied == "user" and who is member) or (denied == "bot" and who is bot)
            else allowed
        )

    monkeypatch.setattr(discord.TextChannel, "permissions_for", permissions)
    profile = UserProfile(channel_tentacle_id="discord", channel_user_id="100")
    source_address = ChannelAddress(
        channel_tentacle_id=tentacle.id,
        chat_type="group",
        chat_id=str(channel.id),
        user_id="100",
        shared=True,
    )
    resolve = AsyncMock(return_value=channel)
    monkeypatch.setattr(tentacle.ink, "resolve_messageable", resolve)
    monkeypatch.setattr(tentacle.ink, "resolve_parent", AsyncMock(return_value=channel))
    assert await tentacle.suggest_addresses(profile) == []
    assert (
        await tentacle.suggest_addresses(
            profile, replace(source_address, chat_type="dm", shared=False)
        )
        == []
    )
    assert (
        await tentacle.suggest_addresses(
            profile,
            replace(source_address, chat_type="thread", channel_thread_id="500"),
        )
        == []
    )
    resolve.assert_not_awaited()
    destinations = await tentacle.suggest_addresses(profile, source_address)
    if denied:
        assert destinations == []
        with pytest.raises(ValueError, match=r"inaccessible|cannot"):
            await tentacle.prepare_address(source_address)
        return
    destination, alternative = destinations
    parent = await tentacle.prepare_address(source_address)
    assert parent == source_address
    fetch_member.reset_mock()
    assert alternative == replace(source_address, chat_id=str(other.id))
    assert destination == source_address
    assert parent.shared
    assert parent.chat_type == "group"
    assert (
        await tentacle.suggest_addresses(
            UserProfile(channel_tentacle_id="unlinked", channel_user_id="100")
        )
        == []
    )
    sent = AsyncMock(return_value=a_message(channel, message_id=700))
    opened = AsyncMock(return_value=a_thread(500, parent_id=channel.id, guild=guild))
    monkeypatch.setattr(
        tentacle.ink, "resolve_messageable", AsyncMock(return_value=channel)
    )
    monkeypatch.setattr(discord.TextChannel, "send", sent)
    monkeypatch.setattr(discord.Message, "create_thread", opened)
    address = await tentacle.start_thread(parent, "Continue here")
    assert address.channel_thread_id == "500"
    fetch_member.assert_awaited_once_with(100)
    # Access is rechecked at execution, after the menu was opened.
    allowed.create_public_threads = False
    with pytest.raises(ValueError, match="cannot start a thread"):
        await tentacle.start_thread(parent, "Continue here")
    with pytest.raises(ValueError, match="cannot start a thread"):
        await tentacle.start_sub_thread(parent, "Continue here")
    sent.assert_awaited_once()
    opened.assert_awaited_once()


@pytest.mark.parametrize("surface", ["dm", "thread"])
async def test_explicit_discord_parent_refuses_what_cannot_hold_a_thread(
    monkeypatch: pytest.MonkeyPatch, surface: str
) -> None:
    ink = DiscordInk(discord.Client(intents=discord.Intents.none()))
    channel = a_dm_channel() if surface == "dm" else a_thread()
    monkeypatch.setattr(ink.client, "get_channel", lambda channel_id: channel)
    fetch = AsyncMock()
    monkeypatch.setattr(discord.Guild, "fetch_member", fetch)

    with pytest.raises(ValueError, match="inaccessible"):
        await ink.prepare_address(ChannelAddress("discord", "group", "400", "100"))

    fetch.assert_not_awaited()
