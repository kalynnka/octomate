from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar, Protocol, runtime_checkable

import anyio
from pydantic_ai import AgentRunResult, AgentRunResultEvent, AgentStreamEvent
from uuid_utils import uuid7

from octomate.schemas.conversation import ConversationKey, UserProfile
from octomate.schemas.events import MessageEvent
from octomate.schemas.segments import ImageSegment
from octomate.tentacles.base import Tentacle
from octomate.utils import guess_image_ext

if TYPE_CHECKING:
    from octomate.base import Octomate

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class DownloadedImage:
    data: bytes
    file_name: str
    content_type: str = ""
    url: str | None = None


@dataclass(frozen=True)
class MarkdownChunker:
    DEFAULT_LIMIT: ClassVar[int] = 12_000

    limit: int = DEFAULT_LIMIT
    natural_min_size: int | None = None

    @property
    def effective_natural_min_size(self) -> int:
        if self.natural_min_size is not None:
            return self.natural_min_size
        return self.limit // 2

    def chunk(self, text: str) -> list[str]:
        if not text:
            return [""]

        chunks: list[str] = []
        remaining = text
        while len(remaining) > self.limit:
            split_at = self.split_index(remaining)
            chunks.append(remaining[:split_at])
            remaining = remaining[split_at:]
        if remaining:
            chunks.append(remaining)
        return chunks

    def split_index(self, text: str) -> int:
        for boundary in (
            self.last_separator_boundary(text, "\n\n"),
            self.last_separator_boundary(text, "\n"),
            self.last_sentence_boundary(text),
            self.last_whitespace_boundary(text),
        ):
            if boundary >= self.effective_natural_min_size:
                return boundary

        return self.last_whitespace_boundary(text) or self.limit

    def last_separator_boundary(self, text: str, separator: str) -> int:
        boundary = 0
        start = 0
        while True:
            index = text.find(separator, start, self.limit)
            if index < 0:
                return boundary
            candidate = index + len(separator)
            if candidate <= self.limit:
                boundary = candidate
            start = index + len(separator)

    def last_sentence_boundary(self, text: str) -> int:
        boundary = 0
        for match in re.finditer(r'[.!?][)"\']?\s+', text[: self.limit]):
            boundary = match.end()
        return boundary

    def last_whitespace_boundary(self, text: str) -> int:
        boundary = 0
        for match in re.finditer(r"\s+", text[: self.limit]):
            boundary = match.end()
        return boundary


@runtime_checkable
class Chromo(Protocol):
    """Two-way translation between platform-native wire data and core schemas."""

    async def sip(self, raw: Any) -> MessageEvent | None: ...

    def squirt(
        self,
        events: AsyncIterator[AgentStreamEvent | AgentRunResultEvent[Any]],
        *,
        reply_to: str | None = None,
    ) -> AsyncIterator[Any]: ...


@runtime_checkable
class Ink(Protocol):
    """Structural protocol for platform API clients."""

    def inspect(self) -> UserProfile: ...

    async def get_user_profile(self, user_id: str) -> UserProfile: ...

    async def upload_media(self, data: bytes) -> str | None:
        """Upload media bytes and return a platform key or URL."""

    async def download_image(
        self,
        seg: ImageSegment,
        message_id: str,
    ) -> DownloadedImage | None:
        """Download an inbound image segment."""

    async def send_message(
        self,
        chat_id: str,
        chat_type: str,
        messages: list[Any],
        reply_to: str | None = None,
        reply_in_thread: bool = False,
    ) -> str | None:
        """Send platform-native message payloads."""


class ChannelTentacle(Tentacle):
    """Base class for IM channels.

    A channel receives native platform events, converts them to MessageEvents,
    and asks Octomate to dispatch the turn to the assigned agent. Outbound
    messages are encoded by Chromo and sent by the configured ink implementation.
    """

    FILES_ROOT: ClassVar[Path] = Path(".octomate/files")

    profile: UserProfile

    def __init__(
        self,
        id: str,
        octomate: Octomate | None,
        *,
        ink: Ink,
        chromo: Chromo,
        agent_id: str = "inkling",
        mention_only: bool = True,
    ) -> None:
        super().__init__(id=id, octomate=octomate)
        self.ink = ink
        self.chromo = chromo
        self.agent_id = agent_id
        self.mention_only = mention_only
        self.user_profiles: dict[str, UserProfile] = {}

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

    async def ingest(self, raw: Any) -> None:
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
            if (
                self.mention_only
                and key.is_group
                and not event.is_at(self.profile.user_id)
            ):
                logger.debug(
                    "Channel %s: ignored unmentioned group event %s",
                    self.id,
                    key,
                )
                return
            if self.octomate is None:
                raise RuntimeError(f"channel {self.id!r} is not attached to Octomate")
            await self.octomate.kick(key, [event], agent_id=self.agent_id)
        except Exception:
            logger.exception("Channel %s: error in ingest", self.id)

    async def respond(
        self,
        key: ConversationKey,
        events: AsyncIterator[AgentStreamEvent | AgentRunResultEvent[Any]],
        *,
        source_events: list[MessageEvent] | None = None,
    ) -> None:
        """Respond to a conversation with pydantic-ai stream events/results."""
        chat_id = key.chat_id or key.user_id
        chat_type = key.chat_type
        reply_to: str | None = key.thread_id or None
        async for message in self.chromo.squirt(events, reply_to=reply_to):
            await self.ink.send_message(
                chat_id,
                chat_type,
                [message],
                reply_to,
            )

    async def respond_text(
        self,
        key: ConversationKey,
        text: str,
        *,
        source_events: list[MessageEvent] | None = None,
    ) -> None:
        """Respond with synthetic text through the normal channel adapter."""

        async def events() -> AsyncIterator[
            AgentStreamEvent | AgentRunResultEvent[Any]
        ]:
            yield AgentRunResultEvent(AgentRunResult(text))

        await self.respond(key, events(), source_events=source_events)

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
