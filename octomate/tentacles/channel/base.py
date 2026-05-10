from __future__ import annotations

import asyncio
import logging
from abc import abstractmethod
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any, ClassVar, Protocol, runtime_checkable

import anyio

from octomate.schemas.actions import AgentMessage
from octomate.schemas.events import MessageEvent
from octomate.schemas.segments import (
    ImageSegment,
    MessageSegment,
    ReplySegment,
)
from octomate.schemas.session import SessionKey, UserProfile
from octomate.tentacles.base import Octopus, Tentacle
from octomate.tentacles.channel.feelers import Feelers

logger = logging.getLogger(__name__)


@runtime_checkable
class Chromo(Protocol):
    """Inbound translator: platform-native wire format → MessageEvent."""

    async def sip(self, raw: Any) -> MessageEvent | None: ...


@runtime_checkable
class Ink(Protocol):
    """Structural protocol for platform API clients — identity, media, send.

    `send_message` owns the segments → platform-wire encoding internally;
    callers hand off raw `MessageSegment`s and the implementation decides how
    to render them for its platform.
    """

    def inspect(self) -> UserProfile: ...

    async def get_user_profile(self, user_id: str) -> UserProfile: ...

    async def upload_media(self, data: bytes) -> str | None:
        """Upload media bytes, return a platform key/URL or None on failure."""
        ...

    async def download_media(
        self, resource_id: str, **kwargs: Any
    ) -> tuple[bytes, str] | None:
        """Download media by platform resource ID. Returns (data, filename) or None."""
        ...

    async def send_message(
        self,
        chat_id: str,
        chat_type: str,
        segments: list[MessageSegment],
        reply_to: str | None = None,
        reply_in_thread: bool = False,
    ) -> str | None:
        """Send segments to the platform. Returns first message ID or None."""
        ...


class ChannelTentacle(Tentacle):
    """A tentacle wrapping a single IM platform connection.

    Receives events from the IM, resolves media, kicks the octopus with each
    inbound event, and exposes outbound APIs (`twitch` for one-shot, `wave`
    for streaming).
    """

    FILES_ROOT: ClassVar[Path] = Path(".octomate/files")

    profile: UserProfile
    ink: Ink
    chromo: Chromo
    feelers: Feelers

    def __init__(self, id: str, octopus: Octopus) -> None:
        super().__init__(id, octopus)
        self.profile = self.ink.inspect()
        logger.info(
            "Tentacle %s: probed as %s (%s)",
            self.id,
            self.profile.user_id,
            self.profile.name,
        )

    @property
    def name(self) -> str:
        return self.profile.name

    @abstractmethod
    async def activate(self) -> None: ...

    @abstractmethod
    async def deactivate(self) -> None: ...

    async def ingest(self, raw: Any) -> None:
        """Inbound: decode → enrich sender → resolve media → kick octopus."""
        try:
            event = await self.chromo.sip(raw)
            if event is None:
                return
            event.tentacle_id = self.id
            event.self_id = self.profile.user_id
            event.sender = await self.ink.get_user_profile(event.user_id)
            await self.submerge(event)
            key = SessionKey(
                channel_tentacle_id=self.id,
                chat_type=event.chat_type,
                chat_id=event.chat_id,
                user_id=event.user_id,
                thread_id=event.thread_id,
            )
            await self.octopus.kick(key, [event])
        except Exception:
            logger.exception("Tentacle %s: error in ingest", self.id)

    async def twitch(self, key: SessionKey, message: AgentMessage) -> None:
        """One-shot send: a single finalized AgentMessage to the platform.

        Each ReplySegment retargets the reply for the segments that follow.
        A second reply encountered while segments are buffered flushes the
        buffer under the previous target before accumulation restarts.
        """
        await self.emerge(message.segments)
        chat_id = str(key.chat_id or key.user_id)
        chat_type = "group" if key.is_group else "private"
        reply_to: str | None = key.thread_id or None
        segments: list[MessageSegment] = []
        for seg in message.segments:
            if isinstance(seg, ReplySegment):
                reply_to = seg.data["id"]
                break
            segments.append(seg)
        if segments:
            await self.ink.send_message(
                chat_id,
                chat_type,
                segments,
                reply_to,
            )

    async def wave(
        self,
        key: SessionKey,
        messages: AsyncIterator[AgentMessage],
    ) -> None:
        """Stream: handle a generator of messages — channel decides per-event handling.

        Default: iterate and twitch each. Smart channels override to batch into
        a single live-updated platform message instead of N separate sends.
        """
        async for message in messages:
            await self.twitch(key, message)

    async def submerge(self, event: MessageEvent) -> None:
        """Resolve inbound media: download images from the event to local storage."""
        pending = [seg for seg in event.segments if isinstance(seg, ImageSegment)]
        if not pending:
            return
        save = self.den(event)
        message_id = str(event.message_id)
        await anyio.Path(save).mkdir(parents=True, exist_ok=True)
        async with asyncio.TaskGroup() as tg:
            for seg in pending:
                tg.create_task(self.absorb(seg, save, message_id))

    async def emerge(self, segments: list[MessageSegment]) -> None:
        """Prepare outbound media: upload images before sending."""
        for seg in segments:
            if isinstance(seg, ImageSegment):
                await self.secrete(seg)

    @abstractmethod
    async def absorb(self, seg: ImageSegment, save_dir: Path, message_id: str) -> None:
        """Download a single inbound image to save_dir."""
        ...

    @abstractmethod
    async def secrete(self, seg: ImageSegment) -> None:
        """Prepare a single outbound image for sending."""
        ...

    def den(self, event: MessageEvent) -> Path:
        subdir = event.chat_id if event.chat_type == "group" else event.user_id
        return self.FILES_ROOT / self.id / subdir
