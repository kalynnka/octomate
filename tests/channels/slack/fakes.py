"""Slack channel test fakes: recording inks, a fake web client, and the
`SlackTentacle` builder the streaming/consume tests share."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import cast

from pydantic import SecretStr
from slack_sdk.models.messages.chunk import Chunk
from slack_sdk.web.async_chat_stream import AsyncChatStream

from octomate.config import SlackChannelConfig, SlackStreamConfig
from octomate.managers.deferred import DeferredActionManager
from octomate.schemas.conversation import ChannelAddress, UserProfile
from octomate.schemas.segments import ImageSegment
from octomate.tentacles.channel.base import DownloadedImage, Ink
from octomate.tentacles.channel.feelers.base import Feelers
from octomate.tentacles.channel.feelers.output import (
    DefaultMarkdownFeeler,
    DefaultSegmentsFeeler,
)
from octomate.tentacles.channel.slack import SlackChromo, SlackTentacle
from octomate.tentacles.channel.slack.feelers.approvals import SlackApprovalFeeler
from octomate.tentacles.channel.slack.feelers.output import SlackTimelineFeeler
from octomate.tentacles.channel.slack.feelers.questions import SlackAskQuestionFeeler
from octomate.tentacles.channel.slack.ink import SlackInk as SlackInkType
from octomate.tentacles.channel.slack.schema import SlackBlock, SlackOutboundMessage
from octomate.types.json import JsonObject


@dataclass
class FakeSlackStream:
    appends: list[str] = field(default_factory=list)
    chunks: list[list[Chunk]] = field(default_factory=list)
    stopped: bool = False


@dataclass
class FakeSlackInk(Ink[SlackOutboundMessage]):
    streams: list[dict[str, str | None]] = field(default_factory=list)
    stream_objects: list[FakeSlackStream] = field(default_factory=list)
    appends: list[str] = field(default_factory=list)
    stream_chunks: list[list[Chunk]] = field(default_factory=list)
    stops: list[str | None] = field(default_factory=list)
    finals: list[dict[str, str | None]] = field(default_factory=list)
    statuses: list[str] = field(default_factory=list)
    sent: list[tuple[str, str, list[SlackOutboundMessage], str | None]] = field(
        default_factory=list
    )
    uploads: list[tuple[str, bytes, str, str | None]] = field(default_factory=list)

    async def inspect(self) -> UserProfile:
        return UserProfile(user_id="bot", name="Bot")

    async def get_user_profile(self, user_id: str) -> UserProfile:
        return UserProfile(user_id=user_id, name=user_id)

    async def upload_media(self, data: bytes) -> str | None:
        return None

    async def upload_image(
        self,
        *,
        channel: str,
        data: bytes,
        filename: str,
        thread_ts: str | None = None,
    ) -> str | None:
        self.uploads.append((channel, data, filename, thread_ts))
        return f"file-{len(self.uploads)}"

    async def download_image(
        self,
        seg: ImageSegment,
        message_id: str,
    ) -> DownloadedImage | None:
        return None

    async def start_stream(
        self,
        channel: str,
        thread_ts: str,
        *,
        recipient_user_id: str | None = None,
        recipient_team_id: str | None = None,
        task_display_mode: str | None = None,
    ) -> FakeSlackStream:
        stream = {
            "channel": channel,
            "thread_ts": thread_ts,
            "recipient_user_id": recipient_user_id,
            "recipient_team_id": recipient_team_id,
        }
        if task_display_mode is not None:
            stream["task_display_mode"] = task_display_mode
        self.streams.append(stream)
        stream_object = FakeSlackStream()
        self.stream_objects.append(stream_object)
        return stream_object

    @asynccontextmanager
    async def open_stream(
        self,
        channel: str,
        thread_ts: str,
        *,
        recipient_user_id: str | None = None,
        recipient_team_id: str | None = None,
        task_display_mode: str | None = None,
    ) -> AsyncIterator[FakeSlackStream]:
        stream = await self.start_stream(
            channel,
            thread_ts,
            recipient_user_id=recipient_user_id,
            recipient_team_id=recipient_team_id,
            task_display_mode=task_display_mode,
        )
        try:
            yield stream
        finally:
            await self.stop_stream(stream)

    async def append_stream(self, stream: FakeSlackStream, markdown_text: str) -> None:
        stream.appends.append(markdown_text)
        self.appends.append(markdown_text)

    async def append_stream_chunks(
        self,
        stream: FakeSlackStream,
        chunks: list[Chunk],
    ) -> None:
        stream.chunks.append(chunks)
        self.stream_chunks.append(chunks)

    async def stop_stream(
        self,
        stream: FakeSlackStream,
        *,
        markdown_text: str | None = None,
    ) -> str:
        stream.stopped = True
        self.stops.append(markdown_text)
        return "stream-ts"

    async def set_assistant_status(
        self,
        channel: str,
        thread_ts: str,
        status: str,
    ) -> None:
        self.statuses.append(status)

    async def stream_markdown(
        self,
        channel: str,
        thread_ts: str,
        markdown_text: str,
        *,
        recipient_user_id: str | None = None,
        recipient_team_id: str | None = None,
    ) -> str:
        self.finals.append(
            {
                "channel": channel,
                "thread_ts": thread_ts,
                "markdown_text": markdown_text,
                "recipient_user_id": recipient_user_id,
                "recipient_team_id": recipient_team_id,
            }
        )
        return "stream-ts"

    async def send_message(
        self,
        chat_id: str,
        chat_type: str,
        messages: list[SlackOutboundMessage],
        reply_to: str | None = None,
        reply_in_thread: bool = False,
    ) -> str:
        self.sent.append((chat_id, chat_type, messages, reply_to))
        return "fallback-ts"


@dataclass
class FakeSlackBlocksInk:
    """The lighter ink the block-kit feelers tests need: sends + card updates,
    with an optional shared `events` log to assert update/kick ordering."""

    sent: list[tuple[str, str, list[SlackOutboundMessage], str | None]] = field(
        default_factory=list
    )
    updates: list[tuple[str, str, str, list[SlackBlock]]] = field(default_factory=list)
    events: list[str] | None = None

    async def send_message(
        self,
        chat_id: str,
        chat_type: str,
        messages: list[SlackOutboundMessage],
        reply_to: str | None = None,
    ) -> str:
        self.sent.append((chat_id, chat_type, messages, reply_to))
        return f"slack-{len(self.sent)}"

    async def update_message(
        self,
        channel: str,
        message_ts: str,
        *,
        text: str,
        blocks: list[SlackBlock],
    ) -> None:
        self.updates.append((channel, message_ts, text, blocks))
        if self.events is not None:
            self.events.append("update")


def slack_channel(
    ink: FakeSlackInk, deferred_actions: DeferredActionManager | None = None
) -> SlackTentacle:
    channel = object.__new__(SlackTentacle)
    channel.id = "slack"
    channel.ink = cast(SlackInkType, ink)
    channel.chromo = SlackChromo()
    channel.config = SlackChannelConfig(
        app_id="A-test",
        bot_token=SecretStr("xoxb-test"),
        app_token=SecretStr("xapp-test"),
        stream=SlackStreamConfig(flush_interval=0),
    )
    compose_slack_feelers(channel, deferred_actions)
    return channel


def compose_slack_feelers(
    channel: SlackTentacle, deferred_actions: DeferredActionManager | None = None
) -> None:
    ink = cast(SlackInkType, channel.ink)
    chromo = cast(SlackChromo, channel.chromo)
    markdown_feeler = DefaultMarkdownFeeler(ink=ink, chromo=chromo)
    approvals = SlackApprovalFeeler(ink)
    ask_questions = SlackAskQuestionFeeler(ink)
    actions = deferred_actions or DeferredActionManager()
    channel.feelers = Feelers(
        markdown=markdown_feeler,
        timeline=SlackTimelineFeeler(
            ink=ink,
            chromo=chromo,
            ask_questions=ask_questions,
            approvals=approvals,
            deferred_actions=actions,
        ),
        segments=DefaultSegmentsFeeler(ink=ink, chromo=chromo),
        approvals=approvals,
        ask_questions=ask_questions,
    )


def slack_key() -> ChannelAddress:
    return ChannelAddress(
        channel_tentacle_id="slack",
        chat_type="group",
        chat_id="C1",
        user_id="U1",
        thread_id="1710000000.000100",
    )


class FakeSlackClient:
    def __init__(self) -> None:
        self.streams: list[JsonObject] = []
        self.uploads: list[JsonObject] = []

    async def chat_stream(
        self,
        *,
        buffer_size: int,
        channel: str,
        thread_ts: str,
        recipient_user_id: str | None = None,
        recipient_team_id: str | None = None,
        task_display_mode: str | None = None,
    ) -> AsyncChatStream:
        self.streams.append(
            {
                "buffer_size": buffer_size,
                "channel": channel,
                "thread_ts": thread_ts,
                "recipient_user_id": recipient_user_id,
                "recipient_team_id": recipient_team_id,
                "task_display_mode": task_display_mode,
            }
        )
        return cast(AsyncChatStream, SimpleNamespace())

    async def files_upload_v2(
        self,
        *,
        channel: str,
        content: str,
        filename: str,
        title: str,
        snippet_type: str,
        initial_comment: str,
        thread_ts: str | None = None,
    ) -> JsonObject:
        self.uploads.append(
            {
                "channel": channel,
                "content": content,
                "filename": filename,
                "title": title,
                "snippet_type": snippet_type,
                "initial_comment": initial_comment,
                "thread_ts": thread_ts,
            }
        )
        return {"file": {"permalink": "https://slack/files/1"}}
