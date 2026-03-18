from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, ClassVar, Literal, Protocol, runtime_checkable

import anyio

from octomate.schemas.events import GroupMessageEvent
from octomate.schemas.segments import ImageSegment
from octomate.schemas.session import SessionKey, UserProfile

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from anyio.abc import TaskGroup

    from octomate.octopus import Octopus
    from octomate.schemas.events import MessageEvent
    from octomate.schemas.segments import AgentSegment

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
        """The bot's platform ID."""
        return self.profile.user_id

    @property
    def name(self) -> str:
        """The bot's display name."""
        return self.profile.name

    @abstractmethod
    async def activate(self) -> None:
        """Start the tentacle: connect to the IM and begin receiving events."""
        ...

    @abstractmethod
    async def deactivate(self) -> None:
        """Gracefully shut down the connection and release resources."""
        ...

    def inspect(self) -> UserProfile:
        """Fetch own identity from the IM platform (sync). Called during __init__."""
        profile = self.ink.inspect()
        logger.info(
            "Tentacle %s: probed as %s (%s)", self.tag, profile.user_id, profile.name
        )
        return profile

    async def get_user_profile(self, user_id: str) -> UserProfile:
        cached = self.user_profiles.get(user_id)
        if cached is not None:
            return cached
        profile = await self.ink.get_user_profile(user_id)
        self.user_profiles[user_id] = profile
        return profile

    async def twitch(self, target: SendTarget, segments: list[AgentSegment]) -> None:
        """Dispatch an outbound action: resolve media, then squirt the message out."""
        await self.emerge(segments)
        await self.splash(target, segments)

    async def submerge(self, event: MessageEvent) -> None:
        """Resolve inbound media: download images from the event to local storage."""
        pending = [seg for seg in event.message if isinstance(seg, ImageSegment)]
        if not pending:
            return
        save = self.den(event)
        message_id = str(event.message_id)
        await anyio.Path(save).mkdir(parents=True, exist_ok=True)
        async with anyio.create_task_group() as tg:
            for seg in pending:
                tg.start_soon(self.absorb, seg, save, message_id)

    async def emerge(self, segments: list[AgentSegment]) -> None:
        """Prepare outbound media: process images in segments before sending."""
        for seg in segments:
            if isinstance(seg, ImageSegment):
                await self.secrete(seg)

    @abstractmethod
    async def splash(self, target: SendTarget, segments: list[AgentSegment]) -> None:
        """Send a resolved message to the target chat via the IM's protocol."""
        ...

    @abstractmethod
    async def absorb(self, seg: ImageSegment, save_dir: Path, message_id: str) -> None:
        """Download a single inbound image to save_dir.
        Must update seg.data.file to the local path on success."""
        ...

    @abstractmethod
    async def secrete(self, seg: ImageSegment) -> None:
        """Prepare a single outbound image for sending
        (e.g. base64 encode, upload to IM). Must update seg.data accordingly."""
        ...

    def den(self, event: MessageEvent) -> Path:
        """Compute the local storage directory for an event's media files."""
        if isinstance(event, GroupMessageEvent):
            subdir = f"{event.group_id}"
        else:
            subdir = f"{event.user_id}"
        return self.FILES_ROOT / self.tag / subdir


class MessageBuffer:
    _flush_delay: float
    _handler: Callable[[SessionKey, list[MessageEvent]], Awaitable[None]]
    _buckets: defaultdict[SessionKey, list[MessageEvent]]
    _pending: set[SessionKey]
    _tg: TaskGroup | None

    def __init__(
        self,
        flush_delay: float,
        handler: Callable[[SessionKey, list[MessageEvent]], Awaitable[None]],
    ) -> None:
        self._flush_delay = flush_delay
        self._handler = handler
        self._buckets: defaultdict[SessionKey, list[MessageEvent]] = defaultdict(list)
        self._pending: set[SessionKey] = set()
        self._tg: TaskGroup | None = None

    def bind(self, tg: TaskGroup) -> None:
        self._tg = tg

    def push(self, event: MessageEvent) -> None:
        key = event.session_key
        self._buckets[key].append(event)
        if key not in self._pending:
            self._pending.add(key)
            if self._tg is None:
                raise RuntimeError("MessageBuffer.bind() must be called before push()")
            self._tg.start_soon(self._flush_after_delay, key)

    async def _flush_after_delay(self, key: SessionKey) -> None:
        await anyio.sleep(self._flush_delay)
        self._pending.discard(key)
        batch = self._buckets.pop(key, [])
        if not batch:
            return
        try:
            await self._handler(key, batch)
        except Exception:
            logger.exception("Error handling batch for %s", key)
