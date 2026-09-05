from __future__ import annotations

import json
from pathlib import Path

import discord
import pytest

from octomate.schemas.segments import (
    AtData,
    AtSegment,
    FileData,
    FileSegment,
    ImageData,
    ImageSegment,
    MarkdownSegment,
    ReplySegment,
    TextSegment,
)
from octomate.tentacles.discord import DiscordChromo
from octomate.tentacles.discord.schema import DiscordOutboundMessage
from octomate.types.conversations import ChatType
from tests.channels.discord.fakes import (
    a_dm_channel,
    a_message,
    a_text_channel,
    a_thread,
    a_user,
    an_attachment,
)


@pytest.mark.parametrize(
    ("channel", "chat_type", "chat_id", "channel_thread_id", "shared"),
    [
        (a_dm_channel(300), "dm", "300", None, False),
        (a_text_channel(400), "group", "400", None, True),
        (a_thread(500, parent_id=400), "thread", "400", "500", True),
    ],
)
async def test_discord_chromo_maps_supported_surfaces(
    channel: discord.DMChannel | discord.TextChannel | discord.Thread,
    chat_type: ChatType,
    chat_id: str,
    channel_thread_id: str | None,
    shared: bool,
) -> None:
    event = await DiscordChromo().sip(a_message(channel))

    assert event is not None
    assert event.chat_type == chat_type
    assert event.chat_id == chat_id
    assert event.channel_thread_id == channel_thread_id
    assert event.shared is shared
    assert event.message_id == "175928847299117063"
    assert event.user_id == "100"
    assert event.timestamp > 0


async def test_discord_chromo_decodes_reply_mentions_and_images_in_order() -> None:
    channel = a_thread(500, parent_id=400)
    bot = a_user(42, name="Octomate", bot=True)
    referenced = a_message(channel, message_id=175928847299117000, author=bot)
    reference = discord.MessageReference(
        message_id=referenced.id,
        channel_id=channel.id,
        guild_id=channel.guild.id,
    )
    reference.resolved = referenced
    alice = a_user(101, name="Alice")
    bob = a_user(102, name="Bob")
    image = an_attachment(description="diagram")
    document = an_attachment(
        601,
        filename="report.pdf",
        url="https://cdn.discordapp.com/report.pdf",
        content_type="application/pdf",
    )

    event = await DiscordChromo().sip(
        a_message(
            channel,
            message_type=discord.MessageType.reply,
            content="before <@101> between <@!102> after <@&900> @everyone",
            mentions=[bob, alice],
            attachments=[image, document],
            reference=reference,
        )
    )

    assert event is not None
    assert event.reply_id == str(referenced.id)
    assert event.replies_to("42")
    assert not event.is_at("42")
    assert [type(segment) for segment in event.segments] == [
        ReplySegment,
        TextSegment,
        AtSegment,
        TextSegment,
        AtSegment,
        TextSegment,
        ImageSegment,
    ]
    first_mention = event.segments[2]
    second_mention = event.segments[4]
    image_segment = event.segments[-1]
    assert isinstance(first_mention, AtSegment)
    assert isinstance(second_mention, AtSegment)
    assert isinstance(image_segment, ImageSegment)
    assert (first_mention.data.user_id, first_mention.data.name) == ("101", "Alice")
    assert (second_mention.data.user_id, second_mention.data.name) == ("102", "Bob")
    assert event.text_parts()[-1] == " after <@&900> @everyone"
    assert image_segment.data.file == image.url
    assert image_segment.data.summary == "diagram"

    snapshot = json.loads(event.raw)
    assert snapshot["channel_id"] == "500"
    assert snapshot["reference"]["author_id"] == "42"
    assert snapshot["attachments"][1]["filename"] == "report.pdf"
    assert "_state" not in event.raw


async def test_discord_chromo_keeps_an_unresolved_reply_without_addressing() -> None:
    channel = a_text_channel()
    reference = discord.MessageReference(message_id=700, channel_id=channel.id)

    event = await DiscordChromo().sip(
        a_message(
            channel,
            message_type=discord.MessageType.reply,
            reference=reference,
        )
    )

    assert event is not None
    assert event.reply_id == "700"
    assert not event.replies_to("42")
    reply = event.segments[0]
    assert isinstance(reply, ReplySegment)
    assert "user_id" not in reply.data


async def test_discord_chromo_ignores_system_and_malformed_messages() -> None:
    channel = a_text_channel()
    system = await DiscordChromo().sip(
        a_message(channel, message_type=discord.MessageType.pins_add)
    )
    malformed = discord.Message.__new__(discord.Message)
    malformed.type = discord.MessageType.default

    assert system is None
    assert await DiscordChromo().sip(malformed) is None


def test_discord_chromo_chunks_markdown_at_the_platform_limit() -> None:
    text = "x" * 2001

    messages = DiscordChromo().outbound_markdown(text)

    assert [len(message.content) for message in messages] == [2000, 1]
    assert "".join(message.content for message in messages) == text
    assert DiscordChromo().outbound_markdown("") == []


async def test_discord_chromo_preserves_native_files_and_intentional_mentions(
    tmp_path: Path,
) -> None:
    image = tmp_path / "image.png"
    document = tmp_path / "report.pdf"
    segments = [
        MarkdownSegment(data={"text": f"{'x' * 2000}<@900>"}),
        AtSegment(data=AtData(user_id="42", name="Octomate")),
        ImageSegment(data=ImageData(file=str(image))),
        FileSegment(data=FileData(file=str(document), name="report.pdf")),
    ]

    messages = await DiscordChromo().outbound_segments(segments)

    assert messages == [
        DiscordOutboundMessage(
            content="x" * 2000,
            attachment_paths=(image, document),
        ),
        DiscordOutboundMessage(
            content="<@900>\n\n<@42>",
            mentioned_user_ids=("42",),
        ),
    ]
