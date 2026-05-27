from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar, Literal, Protocol, runtime_checkable

import anyio
from pydantic_ai import AgentRunResult, AgentRunResultEvent, AgentStreamEvent
from uuid_utils import uuid7

from octomate.config import ChannelConfig
from octomate.schemas.conversation import ConversationKey, UserProfile
from octomate.schemas.events import MessageEvent
from octomate.schemas.segments import ImageSegment
from octomate.tentacles.base import Tentacle
from octomate.utils import guess_image_ext

if TYPE_CHECKING:
    from octomate.base import Octomate

logger = logging.getLogger(__name__)

ThreadStrategy = Literal["main_only", "flat_thread", "nested_thread"]


@dataclass(frozen=True)
class DownloadedImage:
    data: bytes
    file_name: str
    content_type: str = ""
    url: str | None = None


@runtime_checkable
class Chromo(Protocol):
    """Two-way translation between platform-native wire data and core schemas."""

    async def sip(self, raw: Any) -> MessageEvent | None: ...

    def squirt(
        self,
        result: AgentRunResult[Any],
        *,
        reply_to: str | None = None,
    ) -> list[Any]: ...


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
    thread_strategy: ClassVar[ThreadStrategy] = "main_only"

    profile: UserProfile

    def __init__(
        self,
        id: str,
        octomate: Octomate | None,
        *,
        ink: Ink,
        chromo: Chromo,
        config: ChannelConfig,
    ) -> None:
        super().__init__(id=id, octomate=octomate)
        self.config = config
        self.ink = ink
        self.chromo = chromo
        self.agent_id = config.agent_id
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
            if self.octomate is None:
                raise RuntimeError(f"channel {self.id!r} is not attached to Octomate")
            await self.octomate.kick(key, [event], agent_id=self.agent_id)
        except Exception:
            logger.exception("Channel %s: error in ingest", self.id)

    async def respond(
        self,
        key: ConversationKey,
        events: AsyncIterator[AgentStreamEvent | AgentRunResultEvent[Any]],
    ) -> None:
        """Send a final, non-streaming response for a conversation."""
        chat_id = key.chat_id or key.user_id
        chat_type = key.chat_type
        reply_to: str | None = key.thread_id or None
        result_event: AgentRunResultEvent[Any] | None = None
        async for event in events:
            if isinstance(event, AgentRunResultEvent):
                result_event = event
        if result_event is None:
            return

        for message in self.chromo.squirt(result_event.result, reply_to=reply_to):
            await self.ink.send_message(
                chat_id,
                chat_type,
                [message],
                reply_to,
            )

    async def stream_respond(
        self,
        key: ConversationKey,
        events: AsyncIterator[AgentStreamEvent | AgentRunResultEvent[Any]],
    ) -> None:
        logger.warning(
            "Channel %s does not support streaming responses; falling back to final response",
            self.id,
        )
        await self.respond(key, events)

    async def respond_text(
        self,
        key: ConversationKey,
        text: str,
    ) -> None:
        """Respond with synthetic text through the normal channel adapter."""

        async def events() -> AsyncIterator[
            AgentStreamEvent | AgentRunResultEvent[Any]
        ]:
            yield AgentRunResultEvent(AgentRunResult(text))

        await self.respond(key, events())

    async def start_sub_thread(
        self,
        key: ConversationKey,
        hint_text: str,
    ) -> ConversationKey:
        logger.warning(
            "Channel %s does not support sub-thread startup; using main target",
            self.id,
        )
        await self.respond_text(key, hint_text)
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
