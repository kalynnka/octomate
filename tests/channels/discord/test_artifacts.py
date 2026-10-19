"""Native artifact cards keep their destination and separate ledger delivery."""

from unittest.mock import AsyncMock, Mock

import discord
import pytest
from pydantic import AnyHttpUrl, SecretStr
from pydantic_ai.usage import RunUsage
from uuid_utils.compat import uuid7

from octomate import Octomate
from octomate.capabilities.harness.events import (
    ArtifactAvailableEvent,
    RunEndedEvent,
    RunResultEvent,
    RunStartedEvent,
)
from octomate.config import DiscordChannelConfig
from octomate.database import async_session
from octomate.nerve import ChannelTopic, Impulse
from octomate.schemas.artifacts import ArtifactOverview, McpAppArtifact
from octomate.schemas.conversation import ChannelAddress
from octomate.schemas.segments import MarkdownSegment
from octomate.schemas.thread import ThreadArtifact, ThreadMessage
from octomate.schemas.user import UserProfile
from octomate.tentacles.discord import DiscordTentacle
from octomate.tentacles.discord.feelers.artifacts import DiscordArtifactFeeler
from tests.agent.test_mcp_app_ledger import app_artifact as app_artifact
from tests.channels.discord.fakes import (
    a_dm_channel,
    a_message,
    a_text_channel,
    a_thread,
)
from tests.channels.discord.test_feelers import RecordingDiscordUIInk
from tests.channels.discord.test_streaming import RecordingDiscordInk
from tests.support.nerve import drawing


@pytest.mark.parametrize("mode", ["outside", "stream", "quiet"])
@pytest.mark.parametrize(
    "destination",
    [a_dm_channel(), a_text_channel(), a_thread(), a_thread(private=True)],
    ids=["dm", "channel", "thread", "private-thread"],
)
async def test_card_reaches_discord_with_its_own_ledger_id(
    app_artifact: McpAppArtifact,
    mode: str,
    destination: discord.DMChannel | discord.TextChannel | discord.Thread,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    host = Octomate()
    host.config.artifacts.viewer_origin = AnyHttpUrl("https://console.example")
    channel = DiscordTentacle(
        "discord",
        host,
        config=DiscordChannelConfig(bot_token=SecretStr("test"), agents=["codex"]),
    )
    channel.config.stream.enabled = mode == "stream"
    destination._state = channel.client._connection
    monkeypatch.setattr(
        channel.client,
        "get_channel",
        lambda channel_id: destination if channel_id == destination.id else None,
    )
    send = AsyncMock(return_value={})
    monkeypatch.setattr(channel.client.http, "send_message", send)
    monkeypatch.setattr(
        channel.client._connection,
        "create_message",
        Mock(side_effect=[a_message(destination, message_id=id) for id in (801, 802)]),
    )
    edit = AsyncMock(return_value=a_message(destination, message_id=802))
    monkeypatch.setattr(discord.PartialMessage, "edit", edit)
    monkeypatch.setattr(channel.ink, "typing", RecordingDiscordInk().typing)
    upstream = AsyncMock(side_effect=AssertionError("Cards must not load app HTML"))
    monkeypatch.setattr(host.mcp, "app_resource", upstream)
    address = ChannelAddress("discord", "dm", str(destination.id), "100")
    if isinstance(destination, discord.Thread):
        address = ChannelAddress(
            "discord", "thread", str(destination.parent_id), "100", str(destination.id)
        )
    elif isinstance(destination, discord.TextChannel):
        address = ChannelAddress("discord", "group", str(destination.id), "100")
    await host.users.ensure_profile(
        "discord", UserProfile(channel_user_id="100", user_id=app_artifact.owner_id)
    )
    bot = UserProfile(channel_user_id="42")
    thread = await host.threads.ensure(address)
    entry = await host.threads.record_artifact(
        thread.id, app_artifact, sender=bot, agent_tentacle_id="codex"
    )
    reply = await host.threads.record_outbound(
        address,
        agent_tentacle_id="codex",
        segments=[MarkdownSegment(data={"text": "done"})],
        sender=bot,
    )
    async with channel.ink, drawing(channel):
        if mode != "outside":
            host.nerve.deliver(
                ChannelTopic(channel.id),
                Impulse(RunStartedEvent(address=address), address),
            )
        host.nerve.deliver(
            ChannelTopic(channel.id),
            Impulse(
                ArtifactAvailableEvent(
                    message_id=entry.id, artifact_id=app_artifact.id
                ),
                address,
            ),
        )
        if mode != "outside":
            host.nerve.deliver(
                ChannelTopic(channel.id),
                Impulse(RunResultEvent(output="done", usage=RunUsage()), address),
            )
            host.nerve.deliver(
                ChannelTopic(channel.id),
                Impulse(RunEndedEvent(thread_message_id=reply.id), address),
            )

    assert send.await_count == (1 if mode == "outside" else 2)
    assert all(call.args == (destination.id,) for call in send.await_args_list)
    payload = send.await_args_list[0].kwargs["params"].payload
    assert payload["flags"] & (1 << 15)
    assert payload.get("content") is None
    assert payload["allowed_mentions"] == {
        "parse": [],
        "users": [],
        "replied_user": False,
    }
    [card] = payload["components"]
    assert card["type"] == 17
    assert card["accent_color"] == 0x5865F2
    [section] = card["components"]
    assert section["type"] == 9
    assert [item["content"] for item in section["components"]] == [
        "### ✨ Create diagram",
        "-# Diagrams · Interactive artifact",
    ]
    button = section["accessory"]
    assert button["style"] == discord.ButtonStyle.link.value
    assert button["label"] == "Open artifact"
    assert button["url"] == f"https://console.example/artifacts/{app_artifact.id}"
    assert "custom_id" not in button
    if mode == "stream":
        assert edit.await_args is not None
        assert edit.await_args.kwargs["content"] == "done"
    elif mode == "quiet":
        assert send.await_args_list[1].kwargs["params"].payload["content"] == "done"
    async with async_session() as session:
        stored = await session.get(ThreadArtifact, entry.id)
        assert stored is not None
        assert stored.platform_message_id == "801"
        stored_reply = await session.get(ThreadMessage, reply.id)
        assert stored_reply is not None
        assert stored_reply.platform_message_id == (
            None if mode == "outside" else "802"
        )
    upstream.assert_not_awaited()


async def test_metadata_cannot_replace_the_card_layout_or_mention_people() -> None:
    host = Octomate()
    ink = RecordingDiscordUIInk()
    feeler = DiscordArtifactFeeler(artifacts=host.artifacts, users=host.users, ink=ink)
    await feeler.send(
        ChannelAddress("discord", "dm", "300", "100"),
        ArtifactOverview(
            artifact_id=uuid7(),
            title="**Diagram**\n@everyone " + "x" * 5000,
            provider_name="[Provider](https://example.com)\n@here",
            viewer_url=AnyHttpUrl("https://console.example/artifacts/example"),
        ),
    )
    view = ink.sent[0].message.view
    assert isinstance(view, discord.ui.LayoutView)
    assert view.is_dispatchable() is False
    assert view.timeout is None
    texts = [
        item.content
        for item in view.walk_children()
        if isinstance(item, discord.ui.TextDisplay)
    ]
    assert texts[0].startswith("### ✨ \\*\\*Diagram\\*\\* @\u200beveryone ")
    assert len(texts[0]) < 600
    assert (
        texts[1]
        == "-# \\[Provider](https://example.com) @\u200bhere · Interactive artifact"
    )
    assert all("\n" not in text for text in texts)
