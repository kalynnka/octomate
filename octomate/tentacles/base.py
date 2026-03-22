from __future__ import annotations

import asyncio
import logging
from abc import ABC, abstractmethod
from collections import defaultdict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar, Literal, Protocol, runtime_checkable

import anyio
from pydantic_ai import Agent
from pydantic_ai.tools import DeferredToolRequests

from octomate.schemas.actions import AgentMessage, ConfirmAction
from octomate.schemas.events import MessageEvent
from octomate.schemas.segments import AgentSegment, ImageSegment, ReplySegment
from octomate.schemas.session import SessionKey, UserProfile
from octomate.tentacles.feelers import Feelers

# agents.flick imports agents.surge which imports SendTarget from this module,
# and octopus imports Tentacle from this module — both circular.
if TYPE_CHECKING:
    from octomate.agents.surge import SessionContext
    from octomate.memory.base import OctopusMemory
    from octomate.octopus import Octopus

logger = logging.getLogger(__name__)


@dataclass
class PlatformMessage:
    msg_type: str
    content: str
    metadata: dict[str, Any] = field(default_factory=dict)


@runtime_checkable
class Chromo(Protocol):
    """Two-way translation between platform-native wire format and internal schema."""

    async def sip(self, raw: Any) -> MessageEvent | None: ...

    async def squirt(
        self, segments: list[AgentSegment], *, reply_to: str | None = None
    ) -> list[PlatformMessage]: ...


@dataclass
class SendTarget:
    chat_type: Literal["group", "private"]
    chat_id: int | str
    reply_to: int | str | None = None


@runtime_checkable
class Ink(Protocol):
    """Structural protocol for platform API clients — identity and media only."""

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


class Tentacle(ABC):
    """A tentacle wraps a single IM platform connection and exposes it
    through a unified interface. It receives events from the IM, resolves
    media, pushes events into the Nerve, and listens for outbound actions
    to send back."""

    FILES_ROOT: ClassVar[Path] = Path(".octomate/files")

    tag: str
    octopus: Octopus
    profile: UserProfile
    ink: Ink
    chromo: Chromo
    feelers: Feelers
    flick: Agent[SessionContext, list[AgentMessage] | DeferredToolRequests]
    memory: OctopusMemory
    buffer: MessageBuffer
    user_profiles: dict[str, UserProfile]

    def __init__(
        self,
        tag: str,
        octopus: Octopus,
        flick: Agent[SessionContext, list[AgentMessage] | DeferredToolRequests],
        memory: OctopusMemory,
        flush_delay: float = 0.5,
    ) -> None:
        self.tag = tag
        self.octopus = octopus
        self.flick = flick
        self.memory = memory
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
        """Inbound pipeline: decode → enrich sender → resolve media → triage."""
        try:
            event = await self.chromo.sip(raw)
            if event is None:
                return
            event.tentacle_id = self.tag
            event.self_id = self.profile.user_id
            event.sender = await self.get_user_profile(event.user_id)
            await self.submerge(event)
            await self.triage(event)
        except Exception:
            logger.exception("Tentacle %s: error in ingest", self.tag)

    async def triage(self, event: MessageEvent) -> None:
        from octomate.agents.surge import SessionContext

        key = event.session_key
        ctx = SessionContext(session_key=key, tentacle=self, event=event)
        header = (
            f"[group: {event.chat_id}]"
            if event.chat_type == "group"
            else "[chat: private]"
        )
        user_prompt: list[Any] = [header] + event.to_content_parts()
        result = await self.flick.run(user_prompt, deps=ctx)

        if isinstance(result.output, list) and result.output:
            target = (
                SendTarget("group", key.group_id)
                if key.group_id
                else SendTarget("private", key.user_id)
            )
            for msg in result.output:
                await self.twitch(target, msg.segments)

    async def twitch(self, target: SendTarget, segments: list[AgentSegment]) -> None:
        """Outbound pipeline: resolve media → encode → send."""
        await self.emerge(segments)
        reply_to: str | None = str(target.reply_to) if target.reply_to else None
        for seg in segments:
            if isinstance(seg, ReplySegment):
                reply_to = seg.data["id"]
                break
        remaining: list[AgentSegment] = [
            s for s in segments if not isinstance(s, ReplySegment)
        ]
        messages: list[PlatformMessage] = await self.chromo.squirt(remaining)
        await self.send_platform_message(
            str(target.chat_id), target.chat_type, messages, reply_to
        )

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
        return await self.feelers.confirm.send_confirmation(target, action)

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
