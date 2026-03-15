from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from collections import defaultdict
from dataclasses import dataclass
from functools import cached_property
from pathlib import Path
from typing import TYPE_CHECKING, ClassVar, Literal

import anyio
import httpx

from octomate.schemas.events import GroupMessageEvent
from octomate.schemas.segments import ImageSegment
from octomate.schemas.session import SessionKey

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from anyio.abc import TaskGroup

    from octomate.nerve import OctopusNerve
    from octomate.schemas.adaptors import ActionUnion
    from octomate.schemas.events import MessageEvent
    from octomate.schemas.segments import AgentSegment

logger = logging.getLogger(__name__)


@dataclass
class Mask:
    """The identity a tentacle presents on its IM platform — the 'face' it wears."""

    id: str
    name: str


@dataclass
class SendTarget:
    chat_type: Literal["group", "private"]
    chat_id: int | str
    reply_to: int | str | None = None


class Tentacle(ABC):
    """A tentacle wraps a single IM platform connection and exposes it
    through a unified interface. It receives events from the IM, resolves
    media, pushes events into the Nerve, and listens for outbound actions
    to send back."""

    FILES_ROOT: ClassVar[Path] = Path(".octomate/files")

    tag: str
    """Routing identifier — the key used in the Nerve registry and in event/action tentacle_id."""

    mask: Mask
    """The identity (id + display name) this tentacle presents on the IM platform."""

    nerve: OctopusNerve
    """Central nervous system this tentacle is attached to."""

    buffer: MessageBuffer
    """Collects and batches inbound events before delivery."""

    def __init__(self, tag: str, nerve: OctopusNerve, flush_delay: float = 0.5) -> None:
        self.tag = tag
        self.nerve = nerve
        self.mask = self.inspect()
        self.buffer = MessageBuffer(flush_delay=flush_delay, handler=nerve.kick)

    @property
    def id(self) -> str:
        """The bot's platform ID."""
        return self.mask.id

    @property
    def name(self) -> str:
        """The bot's display name."""
        return self.mask.name

    @cached_property
    @abstractmethod
    def ink(self) -> httpx.AsyncClient:
        """The HTTP client for this tentacle's IM REST API."""
        ...

    @abstractmethod
    async def activate(self) -> None:
        """Start the tentacle: connect to the IM and begin receiving events."""
        ...

    @abstractmethod
    async def deactivate(self) -> None:
        """Gracefully shut down the connection and release resources."""
        ...

    @abstractmethod
    def inspect(self) -> Mask:
        """Fetch own identity from the IM platform (sync). Called during __init__."""
        ...

    def sense(self, event: MessageEvent) -> None:
        """Push a received event into the tank for batched delivery to the Nerve."""
        self.buffer.push(event)

    async def twitch(self, action: ActionUnion) -> None:
        """Dispatch an outbound action: resolve media, then squirt the message out."""
        from octomate.schemas.actions import SendGroupMsgAction, SendPrivateMsgAction

        if isinstance(action, SendGroupMsgAction):
            target = SendTarget("group", action.params.group_id, action.params.reply)
            segments = action.params.message
        elif isinstance(action, SendPrivateMsgAction):
            target = SendTarget("private", action.params.user_id, action.params.reply)
            segments = action.params.message
        await self.emerge(segments)
        await self.splash(target, segments)

    async def submerge(self, event: MessageEvent) -> None:
        """Resolve inbound media: download images from the event to local storage."""
        pending = [seg for seg in event.message if isinstance(seg, ImageSegment)]
        if not pending:
            return
        save = self.den(event)
        await anyio.Path(save).mkdir(parents=True, exist_ok=True)
        async with anyio.create_task_group() as tg:
            for seg in pending:
                tg.start_soon(self.absorb, seg, save)

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
    async def absorb(self, seg: ImageSegment, save_dir: Path) -> None:
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
