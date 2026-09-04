from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Coroutine
from dataclasses import dataclass
from typing import ClassVar

import discord
import pytest
from pydantic import SecretStr

from octomate import Octomate
from octomate.config import AgentModelConfig, DiscordChannelConfig
from octomate.schemas.conversation import ChannelAddress
from octomate.tentacles.channel import ChannelSurfaces, build_channel
from octomate.tentacles.discord import (
    DiscordChromo,
    DiscordInk,
    DiscordTentacle,
)
from octomate.tentacles.discord.feelers.output import DiscordTimelineFeeler
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
        self.ready = asyncio.Event()
        self.closed = asyncio.Event()
        self.lifecycle: list[str] = []
        self.listeners: dict[str, DiscordMessageListener] = {}
        self.instances.append(self)

    def event(self, listener: DiscordMessageListener) -> DiscordMessageListener:
        self.lifecycle.append(f"event:{listener.__name__}")
        self.listeners[listener.__name__] = listener
        return listener

    async def login(self, token: str) -> None:
        self.lifecycle.append("login")
        self.login_token = token
        self.user = FakeDiscordUser()

    async def connect(self, *, reconnect: bool) -> None:
        self.lifecycle.append("connect")
        self.connect_reconnect = reconnect
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
        agents=[AgentModelConfig(agent="inkling", model="test")],
    )


def test_build_channel_composes_discord_components(
    config: DiscordChannelConfig,
) -> None:
    channel = build_channel("discord-main", config, Octomate())

    assert isinstance(channel, DiscordTentacle)
    assert isinstance(channel.ink, DiscordInk)
    assert isinstance(channel.chromo, DiscordChromo)
    assert isinstance(channel.feelers.timeline, DiscordTimelineFeeler)
    assert channel.client.intents.message_content is True
    assert DiscordTentacle.thread_strategy == "flat_thread"
    assert DiscordTentacle.surfaces == ChannelSurfaces(
        sub_thread=True,
        direct_message=True,
    )


async def test_gateway_lifecycle(
    config: DiscordChannelConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    FakeDiscordClient.instances.clear()
    monkeypatch.setattr(discord, "Client", FakeDiscordClient)
    channel = DiscordTentacle("discord-main", Octomate(), config=config)
    client = FakeDiscordClient.instances[0]

    async with channel:
        assert client.lifecycle[:2] == ["event:on_message", "login"]
        assert client.listeners["on_message"] == channel.on_message
        assert client.login_token == "discord-test"
        assert client.connect_reconnect is True
        assert channel.name == "Octomate"
        assert channel.gateway_task is not None

    assert client.closed.is_set()
    assert channel.gateway_task is None


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


async def test_start_sub_thread_creates_a_public_thread_for_a_group(
    config: DiscordChannelConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    channel = DiscordTentacle("discord-main", Octomate(), config=config)
    calls: list[tuple[str, str]] = []

    async def start_public_thread(chat_id: str, hint_text: str) -> str:
        calls.append((chat_id, hint_text))
        return "500"

    monkeypatch.setattr(channel.ink, "start_public_thread", start_public_thread)
    address = ChannelAddress(
        channel_tentacle_id=channel.id,
        chat_type="group",
        chat_id="400",
        user_id="100",
        shared=True,
    )

    result = await channel.start_sub_thread(address, "Continue in a thread")

    assert calls == [("400", "Continue in a thread")]
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
    monkeypatch.setattr(channel.ink, "start_public_thread", reject_public_thread)
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
