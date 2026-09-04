from __future__ import annotations

import discord


def a_user(
    user_id: int = 100,
    *,
    name: str = "Alice",
    bot: bool = False,
) -> discord.User:
    user = discord.User.__new__(discord.User)
    user.id = user_id
    user.name = name
    user.global_name = None
    user.discriminator = "0"
    user.bot = bot
    user.system = False
    return user


def a_guild(guild_id: int = 200) -> discord.Guild:
    guild = discord.Guild.__new__(discord.Guild)
    guild.id = guild_id
    return guild


def a_dm_channel(channel_id: int = 300) -> discord.DMChannel:
    channel = discord.DMChannel.__new__(discord.DMChannel)
    channel.id = channel_id
    return channel


def a_text_channel(
    channel_id: int = 400,
    *,
    guild: discord.Guild | None = None,
) -> discord.TextChannel:
    channel = discord.TextChannel.__new__(discord.TextChannel)
    channel.id = channel_id
    channel.guild = guild or a_guild()
    return channel


def a_thread(
    thread_id: int = 500,
    *,
    parent_id: int = 400,
    guild: discord.Guild | None = None,
) -> discord.Thread:
    channel = discord.Thread.__new__(discord.Thread)
    channel.id = thread_id
    channel.parent_id = parent_id
    channel.guild = guild or a_guild()
    return channel


def an_attachment(
    attachment_id: int = 600,
    *,
    filename: str = "image.png",
    url: str = "https://cdn.discordapp.com/image.png",
    content_type: str | None = "image/png",
    size: int = 123,
    description: str | None = None,
) -> discord.Attachment:
    attachment = discord.Attachment.__new__(discord.Attachment)
    attachment.id = attachment_id
    attachment.filename = filename
    attachment.url = url
    attachment.content_type = content_type
    attachment.size = size
    attachment.description = description
    return attachment


def a_message(
    channel: discord.DMChannel | discord.TextChannel | discord.Thread,
    *,
    message_id: int = 175928847299117063,
    message_type: discord.MessageType = discord.MessageType.default,
    author: discord.User | None = None,
    content: str = "hello",
    mentions: list[discord.User] | None = None,
    attachments: list[discord.Attachment] | None = None,
    reference: discord.MessageReference | None = None,
) -> discord.Message:
    message = discord.Message.__new__(discord.Message)
    message.id = message_id
    message.type = message_type
    message.author = author or a_user()
    message.content = content
    message.mentions = list[discord.User | discord.Member](mentions or [])
    message.attachments = attachments or []
    message.reference = reference
    message.channel = channel
    message.guild = None if isinstance(channel, discord.DMChannel) else channel.guild
    return message
