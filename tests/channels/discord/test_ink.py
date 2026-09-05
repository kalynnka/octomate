from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

import discord
import httpx
import pytest

from octomate.schemas.segments import ImageData, ImageSegment
from octomate.tentacles.discord import DiscordInk
from octomate.tentacles.discord.schema import DiscordOutboundMessage
from tests.channels.discord.fakes import (
    a_client_user,
    a_dm_channel,
    a_message,
    a_text_channel,
    a_thread,
    a_user,
)


@pytest.fixture
def client() -> discord.Client:
    return discord.Client(intents=discord.Intents.none())


@dataclass(frozen=True)
class SendCall:
    destination_id: int
    content: str | None
    files: tuple[discord.File, ...]
    files_were_open: tuple[bool, ...]
    allowed_mentions: discord.AllowedMentions
    reference: discord.PartialMessage | None
    mention_author: bool
    view: discord.ui.View | discord.ui.LayoutView | None


def mentioned_user_ids(mentions: discord.AllowedMentions) -> list[int]:
    users = mentions.users
    assert not isinstance(users, bool)
    return [user.id for user in users]


@pytest.mark.parametrize(
    ("channel", "chat_type"),
    [
        (a_dm_channel(300), "dm"),
        (a_text_channel(400), "group"),
    ],
)
async def test_send_uses_chat_id_outside_threads(
    client: discord.Client,
    monkeypatch: pytest.MonkeyPatch,
    channel: discord.DMChannel | discord.TextChannel,
    chat_type: str,
) -> None:
    requested_channels: list[int] = []
    destinations: list[int] = []

    def get_channel(
        channel_id: int,
    ) -> discord.DMChannel | discord.TextChannel | None:
        requested_channels.append(channel_id)
        return channel if channel_id == channel.id else None

    async def send(
        destination: discord.DMChannel | discord.TextChannel,
        content: str | None = None,
        *,
        allowed_mentions: discord.AllowedMentions,
        mention_author: bool = True,
    ) -> discord.Message:
        destinations.append(destination.id)
        assert content == "hello"
        assert allowed_mentions.everyone is False
        assert mention_author is False
        return a_message(destination, message_id=800)

    monkeypatch.setattr(client, "get_channel", get_channel)
    monkeypatch.setattr(type(channel), "send", send)

    message_id = await DiscordInk(client).send_message(
        str(channel.id),
        chat_type,
        [DiscordOutboundMessage(content="hello")],
        channel_thread_id="999",
    )

    assert message_id == "800"
    assert requested_channels == [channel.id]
    assert destinations == [channel.id]


async def test_send_targets_thread_replies_once_and_closes_attachments(
    client: discord.Client,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    thread = a_thread(500, parent_id=400)
    thread._state = client._connection
    requested_channels: list[int] = []
    calls: list[SendCall] = []

    def get_channel(channel_id: int) -> discord.Thread | None:
        requested_channels.append(channel_id)
        return thread if channel_id == thread.id else None

    async def send(
        destination: discord.Thread,
        content: str | None = None,
        *,
        files: Sequence[discord.File] = (),
        allowed_mentions: discord.AllowedMentions,
        reference: discord.PartialMessage | None = None,
        mention_author: bool = True,
        view: discord.ui.View | discord.ui.LayoutView | None = None,
    ) -> discord.Message:
        calls.append(
            SendCall(
                destination_id=destination.id,
                content=content,
                files=tuple(files),
                files_were_open=tuple(not file.fp.closed for file in files),
                allowed_mentions=allowed_mentions,
                reference=reference,
                mention_author=mention_author,
                view=view,
            )
        )
        return a_message(destination, message_id=800 + len(calls))

    monkeypatch.setattr(client, "get_channel", get_channel)
    monkeypatch.setattr(discord.Thread, "send", send)
    image = tmp_path / "image.png"
    image.write_bytes(b"image")
    document = tmp_path / "report.pdf"
    document.write_bytes(b"document")
    view = discord.ui.View(timeout=None)
    view.add_item(discord.ui.Button(label="Continue", url="https://example.com"))
    messages = [
        DiscordOutboundMessage(
            content="first <@42>",
            attachment_paths=(image, document),
            mentioned_user_ids=("42",),
            view=view,
        ),
        DiscordOutboundMessage(content="second <@900>"),
    ]

    message_id = await DiscordInk(client).send_message(
        "400",
        "thread",
        messages,
        channel_thread_id="500",
        reply_to="700",
        reply_in_thread=True,
    )

    assert message_id == "801"
    assert requested_channels == [500]
    assert [call.destination_id for call in calls] == [500, 500]
    assert calls[0].reference is not None
    assert calls[0].reference.id == 700
    assert calls[0].reference.channel is thread
    assert calls[1].reference is None
    assert calls[0].files_were_open == (True, True)
    assert calls[0].view is view
    assert calls[1].view is None
    assert all(file.fp.closed for file in calls[0].files)
    assert mentioned_user_ids(calls[0].allowed_mentions) == [42]
    assert mentioned_user_ids(calls[1].allowed_mentions) == []
    for call in calls:
        assert call.allowed_mentions.everyone is False
        assert call.allowed_mentions.roles is False
        assert call.allowed_mentions.replied_user is False
        assert call.mention_author is False


async def test_send_layout_view_without_message_content(
    client: discord.Client,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    channel = a_text_channel(400)
    layout = discord.ui.LayoutView(timeout=None)
    layout.add_item(discord.ui.Container(discord.ui.TextDisplay("Question")))
    sent_content: list[str | None] = []

    async def send(
        destination: discord.TextChannel,
        content: str | None = None,
        *,
        allowed_mentions: discord.AllowedMentions,
        mention_author: bool = True,
        view: discord.ui.LayoutView,
    ) -> discord.Message:
        assert destination is channel
        assert allowed_mentions.everyone is False
        assert mention_author is False
        assert view is layout
        sent_content.append(content)
        return a_message(destination, message_id=800)

    monkeypatch.setattr(client, "get_channel", lambda channel_id: channel)
    monkeypatch.setattr(discord.TextChannel, "send", send)

    message_id = await DiscordInk(client).send_message(
        str(channel.id),
        "group",
        [DiscordOutboundMessage(view=layout)],
        channel_thread_id=str(channel.id),
    )

    assert message_id == "800"
    assert sent_content == [None]


async def test_send_propagates_reply_failure_and_still_closes_attachments(
    client: discord.Client,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    channel = a_text_channel()
    channel._state = client._connection
    opened_files: list[discord.File] = []
    calls = 0

    async def fail_send(
        destination: discord.TextChannel,
        content: str | None = None,
        *,
        files: Sequence[discord.File] = (),
        allowed_mentions: discord.AllowedMentions,
        reference: discord.PartialMessage | None = None,
        mention_author: bool = True,
    ) -> discord.Message:
        nonlocal calls
        calls += 1
        opened_files.extend(files)
        assert destination is channel
        assert content == "reply"
        assert allowed_mentions.replied_user is False
        assert reference is not None
        assert mention_author is False
        raise RuntimeError("unknown reply")

    monkeypatch.setattr(client, "get_channel", lambda channel_id: channel)
    monkeypatch.setattr(discord.TextChannel, "send", fail_send)
    attachment = tmp_path / "image.png"
    attachment.write_bytes(b"image")

    with pytest.raises(RuntimeError, match="unknown reply"):
        await DiscordInk(client).send_message(
            str(channel.id),
            "group",
            [DiscordOutboundMessage(content="reply", attachment_paths=(attachment,))],
            channel_thread_id=str(channel.id),
            reply_to="700",
        )

    assert calls == 1
    assert len(opened_files) == 1
    assert opened_files[0].fp.closed


async def test_channel_resolution_uses_cache_then_fetch_and_propagates_missing(
    client: discord.Client,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cached = a_dm_channel(300)
    fetched = a_text_channel(400)
    fetches: list[int] = []

    def get_channel(channel_id: int) -> discord.DMChannel | None:
        return cached if channel_id == cached.id else None

    async def fetch_channel(channel_id: int) -> discord.TextChannel:
        fetches.append(channel_id)
        if channel_id == fetched.id:
            return fetched
        raise LookupError(channel_id)

    monkeypatch.setattr(client, "get_channel", get_channel)
    monkeypatch.setattr(client, "fetch_channel", fetch_channel)
    ink = DiscordInk(client)

    assert await ink.resolve_messageable("300") is cached
    assert await ink.resolve_messageable("400") is fetched
    with pytest.raises(LookupError):
        await ink.resolve_messageable("999")
    assert fetches == [400, 999]


async def test_identity_user_lookup_dm_open_and_standalone_upload(
    client: discord.Client,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bot = a_client_user()
    client._connection.user = bot
    cached = a_user(100, name="Cached")
    fetched = a_user(101, name="Fetched")
    fetches: list[int] = []
    opened_for: list[int] = []

    def get_user(user_id: int) -> discord.User | None:
        return cached if user_id == cached.id else None

    async def fetch_user(user_id: int) -> discord.User:
        fetches.append(user_id)
        return fetched

    async def create_dm(user: discord.User) -> discord.DMChannel:
        opened_for.append(user.id)
        return a_dm_channel(301)

    monkeypatch.setattr(client, "get_user", get_user)
    monkeypatch.setattr(client, "fetch_user", fetch_user)
    monkeypatch.setattr(client, "create_dm", create_dm)
    ink = DiscordInk(client)

    assert (await ink.inspect()).channel_user_id == "42"
    assert (await ink.get_user_profile("100")).name == "Cached"
    assert (await ink.get_user_profile("101")).name == "Fetched"
    assert await ink.open_dm("101") == "301"
    assert await ink.open_dm("") is None
    assert await ink.upload_media(b"image") is None
    assert fetches == [101, 101]
    assert opened_for == [101]


async def test_download_image_returns_typed_media_and_closes_http(
    client: discord.Client,
) -> None:
    async def respond(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == "https://cdn.discordapp.com/signed/image.png?x=1"
        return httpx.Response(
            200,
            content=b"image",
            headers={"content-type": "image/png"},
        )

    http = httpx.AsyncClient(transport=httpx.MockTransport(respond))
    ink = DiscordInk(client, http=http)
    segment = ImageSegment(
        data=ImageData(
            file="https://cdn.discordapp.com/signed/image.png?x=1",
            url="https://cdn.discordapp.com/signed/image.png?x=1",
            name="diagram.png",
        )
    )

    image = await ink.download_image(segment, "700")
    await ink.__aexit__(None, None, None)

    assert image is not None
    assert image.data == b"image"
    assert image.file_name == "diagram.png"
    assert image.content_type == "image/png"
    assert image.url == segment.data.url
    assert http.is_closed


@dataclass
class RecordingTyping:
    entered: bool = False
    exited: bool = False

    async def __aenter__(self) -> None:
        self.entered = True

    async def __aexit__(self, *exc: object) -> None:
        self.exited = True


async def test_edit_and_typing_use_the_resolved_destination(
    client: discord.Client,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    channel = a_text_channel()
    channel._state = client._connection
    edits: list[tuple[int, str | None, discord.AllowedMentions]] = []
    typing = RecordingTyping()

    async def edit(
        message: discord.PartialMessage,
        *,
        content: str | None,
        allowed_mentions: discord.AllowedMentions,
    ) -> discord.Message:
        edits.append((message.id, content, allowed_mentions))
        return a_message(channel, message_id=message.id)

    monkeypatch.setattr(client, "get_channel", lambda channel_id: channel)
    monkeypatch.setattr(discord.PartialMessage, "edit", edit)
    monkeypatch.setattr(discord.TextChannel, "typing", lambda destination: typing)
    ink = DiscordInk(client)

    message_id = await ink.edit_message(
        str(channel.id),
        "700",
        "edited <@42>",
        mentioned_user_ids=("42",),
    )
    async with ink.typing(str(channel.id)):
        assert typing.entered

    assert message_id == "700"
    assert len(edits) == 1
    assert mentioned_user_ids(edits[0][2]) == [42]
    assert typing.exited


async def test_start_public_thread_posts_safe_hint_and_bounds_the_name(
    client: discord.Client,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    channel = a_text_channel()
    sent_hints: list[tuple[str | None, discord.AllowedMentions, bool]] = []
    thread_names: list[str] = []

    async def send(
        destination: discord.TextChannel,
        content: str | None = None,
        *,
        allowed_mentions: discord.AllowedMentions,
        mention_author: bool = True,
    ) -> discord.Message:
        assert destination is channel
        sent_hints.append((content, allowed_mentions, mention_author))
        return a_message(channel, message_id=700)

    async def create_thread(
        message: discord.Message,
        *,
        name: str,
    ) -> discord.Thread:
        assert message.id == 700
        thread_names.append(name)
        return a_thread(500, parent_id=channel.id, guild=channel.guild)

    monkeypatch.setattr(client, "get_channel", lambda channel_id: channel)
    monkeypatch.setattr(discord.TextChannel, "send", send)
    monkeypatch.setattr(discord.Message, "create_thread", create_thread)
    hint = f"# **Plan** {'word ' * 30}<@42>"

    thread_id = await DiscordInk(client).start_public_thread(str(channel.id), hint)

    assert thread_id == "500"
    assert sent_hints[0][0] == hint
    assert mentioned_user_ids(sent_hints[0][1]) == []
    assert sent_hints[0][1].everyone is False
    assert sent_hints[0][2] is False
    assert len(thread_names[0]) == 100
    assert not thread_names[0].startswith("#")
    assert "**" not in thread_names[0]


async def test_start_public_thread_rejects_a_dm(
    client: discord.Client,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    channel = a_dm_channel()
    monkeypatch.setattr(client, "get_channel", lambda channel_id: channel)

    with pytest.raises(TypeError, match="cannot start a public thread"):
        await DiscordInk(client).start_public_thread(str(channel.id), "hint")
