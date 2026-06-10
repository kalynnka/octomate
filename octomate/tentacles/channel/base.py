"""Shared channel primitives.

Type variables:
- RawT: native inbound platform payload accepted by a channel and its chromo.
- MessageT: native outbound platform message payload built and sent by an ink.
"""

from __future__ import annotations

import asyncio
import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, ClassVar, Generic, Literal, TypeAlias, TypeVar

import anyio
import logfire
from pydantic_ai.tools import DeferredToolRequests
from uuid_utils import uuid7

from octomate.config import ChannelConfig
from octomate.schemas.awakes import UserMessageSignal
from octomate.schemas.conversation import ConversationKey, UserProfile
from octomate.schemas.events import MessageEvent
from octomate.schemas.segments import ImageSegment
from octomate.tentacles.base import Tentacle
from octomate.tentacles.channel.feelers.base import Feelers
from octomate.tentacles.channel.feelers.deferred import (
    PlainTextApprovalFeeler,
    PlainTextAskQuestionFeeler,
)
from octomate.tentacles.channel.feelers.output import (
    DefaultEventStreamFeeler,
    DefaultMarkdownFeeler,
    DefaultMarkdownStreamFeeler,
    IMMessageID,
)
from octomate.utils import guess_image_ext

if TYPE_CHECKING:
    from octomate.base import Octomate

logger = logging.getLogger(__name__)

ThreadStrategy = Literal["main_only", "flat_thread", "nested_thread"]
ChannelOutput: TypeAlias = str | DeferredToolRequests | None
MessageT = TypeVar("MessageT")
RawT = TypeVar("RawT")


@dataclass(frozen=True)
class DownloadedImage:
    data: bytes
    file_name: str
    content_type: str = ""
    url: str | None = None


class Chromo(
    ABC,
    Generic[RawT, MessageT],
):
    """Two-way translation between platform-native wire data and core schemas.

    `sip` converts inbound payloads to a `MessageEvent`; `outbound_markdown`
    converts an outbound markdown reply to platform-native message payloads.
    Translation only — the actual send/receive is the ink's job.
    """

    @abstractmethod
    async def sip(self, raw: RawT) -> MessageEvent | None: ...

    @abstractmethod
    def outbound_markdown(self, text: str) -> list[MessageT]:
        """Encode markdown text as platform-native outbound message payloads."""


class Ink(ABC, Generic[MessageT]):
    """Base class for platform API clients (transport only)."""

    @abstractmethod
    def inspect(self) -> UserProfile: ...

    @abstractmethod
    async def get_user_profile(self, user_id: str) -> UserProfile: ...

    @abstractmethod
    async def upload_media(self, data: bytes) -> str | None:
        """Upload media bytes and return a platform key or URL."""

    @abstractmethod
    async def download_image(
        self,
        seg: ImageSegment,
        message_id: str,
    ) -> DownloadedImage | None:
        """Download an inbound image segment."""

    @abstractmethod
    async def send_message(
        self,
        chat_id: str,
        chat_type: str,
        messages: list[MessageT],
        reply_to: str | None = None,
        reply_in_thread: bool = False,
    ) -> IMMessageID | None:
        """Send platform-native message payloads."""


class ChannelTentacle(
    Tentacle[RawT, MessageT],
):
    """Base class for IM channels.

    A channel receives native platform events, converts them to MessageEvents,
    and asks Octomate to dispatch the turn to the assigned agent. Outbound
    messages are encoded by Chromo and sent by the configured ink implementation.
    """

    FILES_ROOT: ClassVar[Path] = Path(".octomate/files")
    thread_strategy: ClassVar[ThreadStrategy] = "main_only"

    profile: UserProfile
    feelers: Feelers[ChannelOutput]
    ink: Ink[MessageT]
    chromo: Chromo[RawT, MessageT]
    config: ChannelConfig

    def __init__(
        self,
        id: str,
        octomate: Octomate,
        *,
        ink: Ink[MessageT],
        chromo: Chromo[RawT, MessageT],
        config: ChannelConfig,
    ) -> None:
        super().__init__(id=id, octomate=octomate)
        self.config = config
        self.ink = ink
        self.chromo = chromo
        self.agent_id = config.agent_id
        self.user_profiles: dict[str, UserProfile] = {}
        markdown_feeler = DefaultMarkdownFeeler(ink=self.ink, chromo=self.chromo)
        self.feelers = Feelers[ChannelOutput](
            markdown=markdown_feeler,
            markdown_stream=DefaultMarkdownStreamFeeler[RawT, MessageT, ChannelOutput](
                ink=self.ink,
                chromo=self.chromo,
            ),
            event_stream=DefaultEventStreamFeeler[RawT, MessageT, ChannelOutput](
                ink=self.ink,
                chromo=self.chromo,
            ),
            approvals=PlainTextApprovalFeeler(markdown_feeler),
            ask_questions=PlainTextAskQuestionFeeler(markdown_feeler),
        )

        self.profile = self.ink.inspect()
        logger.info(
            "Channel %s: probed as %s (%s)",
            self.id,
            self.profile.user_id,
            self.profile.name,
        )

    @property
    def name(self) -> str:
        return self.profile.name

    @logfire.instrument("ChannelTentacle {self.id} ingest")
    async def ingest(self, raw: RawT) -> None:
        """Inbound pipeline: decode, enrich sender, resolve media, dispatch."""
        try:
            event = await self.chromo.sip(raw)
            if event is None:
                return
            event.tentacle_id = self.id
            event.self_id = self.profile.user_id
            event.sender = await self.get_user_profile(event.user_id)
            await self.submerge(event)
            key = ConversationKey(
                channel_tentacle_id=self.id,
                chat_type=event.chat_type,
                chat_id=event.chat_id,
                user_id=event.user_id,
                thread_id=event.thread_id,
            )
            logfire.info(
                "ingest decoded message",
                channel_id=self.id,
                conversation_key=str(key),
                message_id=str(event.message_id),
            )
            if (
                self.config.mention_only
                and key.is_group
                and not event.is_at(self.profile.user_id)
            ):
                logger.debug(
                    "Channel %s: ignored unmentioned group event %s",
                    self.id,
                    key,
                )
                return
            await self.octomate.kick(UserMessageSignal([event]))
        except Exception:
            logger.exception("Channel %s: error in ingest", self.id)

    async def start_sub_thread(
        self,
        key: ConversationKey,
        hint_text: str,
    ) -> ConversationKey:
        logger.warning(
            "Channel %s does not support sub-thread startup; using main target",
            self.id,
        )
        await self.feelers.markdown.present(key, hint_text)
        return key

    async def get_user_profile(self, user_id: str) -> UserProfile:
        cached = self.user_profiles.get(user_id)
        if cached is not None:
            return cached
        profile = await self.ink.get_user_profile(user_id)
        self.user_profiles[user_id] = profile
        return profile

    async def submerge(self, event: MessageEvent) -> None:
        pending = [seg for seg in event.segments if isinstance(seg, ImageSegment)]
        if not pending:
            return
        save = self.den(event)
        message_id = str(event.message_id)
        await anyio.Path(save).mkdir(parents=True, exist_ok=True)
        async with asyncio.TaskGroup() as tg:
            for seg in pending:
                tg.create_task(self.absorb(seg, save, message_id))

    async def absorb(self, seg: ImageSegment, save_dir: Path, message_id: str) -> None:
        """Download one inbound image to save_dir and rewrite seg.data.file."""
        try:
            image = await self.ink.download_image(seg, message_id)
            if image is None:
                return
            ext = guess_image_ext(image.content_type, image.file_name)
            file_path = save_dir / f"{uuid7().hex}{ext}"
            await anyio.Path(file_path).write_bytes(image.data)
            seg.data.file = str(file_path.resolve())
            if image.url is not None:
                seg.data.url = image.url
        except Exception:
            logger.warning(
                "Channel %s: failed to download image", self.id, exc_info=True
            )

    def den(self, event: MessageEvent) -> Path:
        subdir = event.chat_id if event.chat_type == "group" else event.user_id
        return self.FILES_ROOT / self.id / subdir
