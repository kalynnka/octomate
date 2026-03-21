from __future__ import annotations

import asyncio
import logging
from abc import ABC, abstractmethod
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar, Literal, Protocol, runtime_checkable

import anyio

from octomate.schemas.events import MessageEvent
from octomate.schemas.segments import AgentSegment, ImageSegment, ReplySegment
from octomate.schemas.session import SessionKey, UserProfile
from octomate.tentacles.chromo import Chromo, PlatformMessage

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from octomate.octopus import Octopus
    from octomate.schemas.actions import ConfirmAction

logger = logging.getLogger(__name__)


@dataclass
class SendTarget:
    chat_type: Literal["group", "private"]
    chat_id: int | str
    reply_to: int | str | None = None


@runtime_checkable
class Ink(Protocol):
    """Structural protocol for platform API clients."""

    def inspect(self) -> UserProfile: ...

    async def send_message(
        self,
        receive_id: str,
        receive_id_type: str,
        msg_type: str,
        content: str,
    ) -> bool: ...

    async def reply_message(
        self,
        message_id: str,
        msg_type: str,
        content: str,
    ) -> bool: ...

    async def upload_image(self, data: bytes) -> str | None: ...

    async def download_image(
        self, message_id: str, file_key: str
    ) -> tuple[bytes, str] | None: ...

    async def get_user_profile(self, user_id: str) -> UserProfile: ...


class Tentacle(ABC):
    """A tentacle wraps a single IM platform connection and exposes it
    through a unified interface. It receives events from the IM, resolves
    media, pushes events into the Nerve, and listens for outbound actions
    to send back."""

    FILES_ROOT: ClassVar[Path] = Path(".octomate/files")

    tag: str
    profile: UserProfile
    ink: Ink
    chromo: Chromo
    octopus: Octopus
    buffer: MessageBuffer
    user_profiles: dict[str, UserProfile]

    def __init__(self, tag: str, octopus: Octopus, flush_delay: float = 0.5) -> None:
        self.tag = tag
        self.octopus = octopus
        self.profile = self.inspect()
        self.buffer = MessageBuffer(flush_delay=flush_delay, handler=octopus.kick)
        self.user_profiles = {}

    @property
    def id(self) -> str:
        return self.profile.user_id

    @property
    def name(self) -> str:
        return self.profile.name

    @abstractmethod
    async def activate(self) -> None: ...

    @abstractmethod
    async def deactivate(self) -> None: ...

    def inspect(self) -> UserProfile:
        profile = self.ink.inspect()
        logger.info(
            "Tentacle %s: probed as %s (%s)", self.tag, profile.user_id, profile.name
        )
        return profile

    async def ingest(self, raw: Any) -> None:
        """Inbound pipeline: decode → enrich sender → resolve media → buffer."""
        try:
            event = await self.chromo.decode(raw)
            if event is None:
                return
            event.tentacle_id = self.tag
            event.self_id = self.profile.user_id
            event.sender = await self.get_user_profile(event.user_id)
            await self.submerge(event)
            self.buffer.push(event)
        except Exception:
            logger.exception("Tentacle %s: error in ingest", self.tag)

    async def twitch(self, target: SendTarget, segments: list[AgentSegment]) -> None:
        """Outbound pipeline: resolve media → encode → send."""
        await self.emerge(segments)
        reply_to: str | None = str(target.reply_to) if target.reply_to else None
        for seg in segments:
            if isinstance(seg, ReplySegment):
                reply_to = seg.data["id"]
                break
        remaining: list[AgentSegment] = [s for s in segments if not isinstance(s, ReplySegment)]
        messages: list[PlatformMessage] = await self.chromo.encode(remaining)
        await self.send_platform_message(str(target.chat_id), target.chat_type, messages, reply_to)

    @abstractmethod
    async def send_platform_message(
        self,
        chat_id: str,
        chat_type: str,
        messages: list[PlatformMessage],
        reply_to: str | None = None,
    ) -> bool: ...

    async def send_confirmation(
        self, target: SendTarget, action: ConfirmAction
    ) -> bool:
        return False

    async def get_user_profile(self, user_id: str) -> UserProfile:
        cached = self.user_profiles.get(user_id)
        if cached is not None:
            return cached
        profile = await self.ink.get_user_profile(user_id)
        self.user_profiles[user_id] = profile
        return profile

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

    async def emerge(self, segments: list[AgentSegment]) -> None:
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
        return self.FILES_ROOT / self.tag / subdir


class MessageBuffer:
    _flush_delay: float
    _handler: Callable[[SessionKey, list[MessageEvent]], Awaitable[None]]
    _buckets: defaultdict[SessionKey, list[MessageEvent]]
    _pending: set[SessionKey]

    def __init__(
        self,
        flush_delay: float,
        handler: Callable[[SessionKey, list[MessageEvent]], Awaitable[None]],
    ) -> None:
        self._flush_delay = flush_delay
        self._handler = handler
        self._buckets: defaultdict[SessionKey, list[MessageEvent]] = defaultdict(list)
        self._pending: set[SessionKey] = set()

    def push(self, event: MessageEvent) -> None:
        key = event.session_key
        self._buckets[key].append(event)
        if key not in self._pending:
            self._pending.add(key)
            asyncio.create_task(self._flush_after_delay(key))

    async def _flush_after_delay(self, key: SessionKey) -> None:
        await asyncio.sleep(self._flush_delay)
        self._pending.discard(key)
        batch = self._buckets.pop(key, [])
        if not batch:
            return
        try:
            await self._handler(key, batch)
        except Exception:
            logger.exception("Error handling batch for %s", key)
