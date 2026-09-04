from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import ClassVar

import discord
import pytest
from pydantic import SecretStr

from octomate import Octomate
from octomate.config import AgentModelConfig, DiscordChannelConfig
from octomate.tentacles.channel import build_channel
from octomate.tentacles.discord import (
    DiscordChromo,
    DiscordInk,
    DiscordTentacle,
)


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
        self.instances.append(self)

    async def login(self, token: str) -> None:
        self.login_token = token
        self.user = FakeDiscordUser()

    async def connect(self, *, reconnect: bool) -> None:
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
    assert channel.client.intents.message_content is True


async def test_gateway_lifecycle(
    config: DiscordChannelConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    FakeDiscordClient.instances.clear()
    monkeypatch.setattr(discord, "Client", FakeDiscordClient)
    channel = DiscordTentacle("discord-main", Octomate(), config=config)
    client = FakeDiscordClient.instances[0]

    async with channel:
        assert client.login_token == "discord-test"
        assert client.connect_reconnect is True
        assert channel.name == "Octomate"
        assert channel.gateway_task is not None

    assert client.closed.is_set()
    assert channel.gateway_task is None
