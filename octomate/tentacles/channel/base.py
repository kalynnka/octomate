from __future__ import annotations

import asyncio
import logging
from abc import abstractmethod
from collections import defaultdict
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from functools import cached_property
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar, Protocol, runtime_checkable

import anyio
import logfire
from pydantic_ai.toolsets import FunctionToolset

from octomate.schemas.events import MessageEvent
from octomate.schemas.segments import (
    AgentSegment,
    ImageSegment,
    MarkdownSegment,
    ReplySegment,
    TextSegment,
)
from octomate.schemas.session import SessionKey, UserProfile
from octomate.stores.interaction import InteractionStore
from octomate.stores.thread import ThreadStore
from octomate.tentacles.agent.context import SessionContext
from octomate.tentacles.agent.tools import history_toolset
from octomate.tentacles.base import Tentacle
from octomate.tentacles.channel.feelers import NULL_FEELERS, Feelers

# agents.pulse imports AgentTentacle from tentacles.agent.base at runtime (no circular issue).
# agents.pulse only needs ChannelTentacle + StreamSink for TYPE_CHECKING.
if TYPE_CHECKING:
    from octomate.memory.base import OctopusMemory
    from octomate.octopus import Octopus
    from octomate.tentacles.agent.pulse import PulseTentacle

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


class StreamSink:
    """Streaming output sink for agent tentacles.

    Channels provide platform-specific implementations via open_stream().
    """

    """Default StreamSink that sends each append/status as a separate message."""

    tentacle: ChannelTentacle
    key: SessionKey

    def __init__(self, tentacle: ChannelTentacle, key: SessionKey) -> None:
        self.tentacle = tentacle
        self.key = key

    async def set_status(self, status: str) -> None:
        await self.tentacle.twitch(self.key, [TextSegment(data={"text": status})])

    async def append(self, text: str) -> None:
        await self.tentacle.twitch(self.key, [MarkdownSegment(data={"text": text})])

    async def flush(self) -> None:
        pass

    async def post_thinking_block(self, text: str) -> None:
        """Post a collapsible thinking block.

        Default: appends the raw text so platforms without folding support
        still surface the content inline.
        """
        if text:
            await self.append(text)


class ChannelTentacle(Tentacle):
    """A tentacle wrapping a single IM platform connection. It receives events
    from the IM, resolves media, pushes events into the Nerve, and listens
    for outbound actions to send back."""

    FILES_ROOT: ClassVar[Path] = Path(".octomate/files")

    profile: UserProfile
    ink: Ink
    chromo: Chromo
    feelers: Feelers
    pulse: PulseTentacle
    memory: OctopusMemory
    buffer: MessageBuffer
    user_profiles: dict[str, UserProfile]
    threads: ThreadStore
    interactions: InteractionStore

    def __init__(
        self,
        id: str,
        octopus: Octopus,
        pulse: PulseTentacle,
        memory: OctopusMemory,
        flush_delay: float = 0.5,
    ) -> None:
        super().__init__(id, octopus)
        self.pulse = pulse
        self.memory = memory
        self.buffer = MessageBuffer(flush_delay=flush_delay, handler=octopus.kick)
        self.user_profiles = {}
        self.threads = ThreadStore(default_owner=id)
        self.interactions = InteractionStore(threads=self.threads)
        self.feelers = NULL_FEELERS

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

    @logfire.instrument("ChannelTentacle {self.id} call [{key}]")
    async def __call__(self, key: SessionKey, contents: list[MessageEvent]) -> None:
        if not contents:
            return
        try:
            await self.memory.record(key, contents)
            if key.group_id and not any(
                msg.is_at(self.profile.user_id) for msg in contents
            ):
                await self.memory.memo(key, contents, self)
                return
            logger.info(
                "Tentacle %s pulse [%s] (%d messages)", self.id, key, len(contents)
            )
            await self.pulse.run(key, contents)
        except Exception:
            logger.exception("Error in tentacle %s [%s]", self.id, key)

    async def ingest(self, raw: Any) -> None:
        """Inbound pipeline: decode → enrich sender → resolve media → triage."""
        try:
            event = await self.chromo.sip(raw)
            if event is None:
                return
            event.tentacle_id = self.id
            event.self_id = self.profile.user_id
            event.sender = await self.get_user_profile(event.user_id)
            await self.submerge(event)
            self.buffer.push(event)
        except Exception:
            logger.exception("Tentacle %s: error in ingest", self.id)

    async def twitch(self, key: SessionKey, segments: list[AgentSegment]) -> None:
        """Outbound pipeline: resolve media → encode → send."""
        await self.emerge(segments)
        reply_to: str | None = str(key.thread_id) if key.thread_id else None
        for seg in segments:
            if isinstance(seg, ReplySegment):
                reply_to = seg.data["id"]
                break
        remaining: list[AgentSegment] = [
            s for s in segments if not isinstance(s, ReplySegment)
        ]
        messages: list[PlatformMessage] = await self.chromo.squirt(remaining)
        chat_id = key.chat_id or key.group_id or key.user_id
        chat_type = "group" if key.group_id else "private"
        await self.send_platform_message(
            str(chat_id),
            chat_type,
            messages,
            reply_to,
        )

    @abstractmethod
    async def send_platform_message(
        self,
        chat_id: str,
        chat_type: str,
        messages: list[PlatformMessage],
        reply_to: str | None = None,
        reply_in_thread: bool = False,
    ) -> str | None: ...

    @asynccontextmanager
    async def open_stream(self, key: SessionKey) -> AsyncIterator[StreamSink]:
        """Open a streaming sink for progressive output.

        Subclasses override to provide platform-specific streaming (e.g. Slack
        message updates). The default sends each append/status as a separate message.
        """
        sink = StreamSink(tentacle=self, key=key)
        try:
            yield sink
        finally:
            await sink.flush()

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
        return self.FILES_ROOT / self.id / subdir

    @cached_property
    def toolsets(self) -> list[FunctionToolset[SessionContext]]:
        from octomate.tentacles.channel.tools import channel_toolset

        return [channel_toolset(), history_toolset()]


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
