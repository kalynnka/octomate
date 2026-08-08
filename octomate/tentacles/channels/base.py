"""Shared channel primitives.

Type variables:
- RawT: native inbound platform payload accepted by a channel and its chromo.
- MessageT: native outbound platform message payload built and sent by an ink.
"""

from __future__ import annotations

import asyncio
import logging
from abc import ABC, abstractmethod
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import (
    TYPE_CHECKING,
    ClassVar,
    Generic,
    Literal,
    Self,
    TypeAlias,
    TypeVar,
)

import anyio
from opentelemetry import trace
from pydantic_ai.tools import DeferredToolRequests
from uuid_utils import uuid7

from octomate.config import ChannelConfig
from octomate.schemas.awakes import UserMessageSignal
from octomate.schemas.conversation import ChannelAddress
from octomate.schemas.events import MessageEvent
from octomate.schemas.segments import ImageSegment, MessageSegment
from octomate.schemas.user import UserProfile
from octomate.telemetry import channel_logfire
from octomate.tentacles.base import Tentacle
from octomate.tentacles.channels.feelers.base import Feelers
from octomate.tentacles.channels.feelers.deferred import (
    PlainTextApprovalFeeler,
    PlainTextAskQuestionFeeler,
)
from octomate.tentacles.channels.feelers.oauth import PlainTextOAuthFeeler
from octomate.tentacles.channels.feelers.output import (
    DefaultMarkdownFeeler,
    DefaultSegmentsFeeler,
    DefaultTimelineFeeler,
    IMMessageID,
)
from octomate.utils import guess_image_ext

if TYPE_CHECKING:
    from octomate.base import Octomate

logger = logging.getLogger(__name__)

ThreadStrategy = Literal["main_only", "flat_thread"]
ChannelOutput: TypeAlias = str | Sequence[MessageSegment] | DeferredToolRequests | None
MessageT = TypeVar("MessageT")
RawT = TypeVar("RawT")


@dataclass(frozen=True)
class StaticMount:
    """One static-file surface a channel asks the host app to serve.

    Declared as data rather than a Starlette app so the host controls mount
    order: `Octomate.app` mounts these after every router, which is what lets
    a catch-all `path="/"` coexist with the API routes."""

    path: str
    directory: Path
    name: str
    html: bool = True


@dataclass(frozen=True)
class ChannelSurfaces:
    """Which places this channel can open, as opposed to how it routes them.

    `thread_strategy` says how an inbound *threaded* message is routed; these say what
    the platform can be asked to create. They are separate because a channel can want
    thread routing without having threads: the dev UI declares `flat_thread` to skip
    triage and can open nothing.

    Declared per channel class rather than probed — a spell has to know before it runs,
    and asking the platform every turn would spend a round-trip to answer a constant.
    """

    sub_thread: bool = False
    direct_message: bool = False


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

    async def outbound_segments(self, segments: list[MessageSegment]) -> list[MessageT]:
        """Encode output segments as platform-native outbound message payloads. The
        default flattens them to a single markdown body via their `str` form;
        channels with native transport override to ship media inline or to encode a
        mention token a user actually gets pinged by."""
        return self.outbound_markdown("\n\n".join(str(seg) for seg in segments))


class Ink(ABC, Generic[MessageT]):
    """Base class for platform API clients (transport only).

    An ink is an async context manager: the owning tentacle enters it as part
    of its own lifecycle. The base implementation holds no resources; inks
    whose platform calls can share a pooled connection override
    `__aenter__`/`__aexit__` to open and close it.
    """

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *exc: object) -> None: ...

    @abstractmethod
    async def inspect(self) -> UserProfile: ...

    @abstractmethod
    async def get_user_profile(self, user_id: str) -> UserProfile: ...

    @abstractmethod
    async def upload_media(self, data: bytes) -> str | None:
        """Upload media bytes and return a platform address or URL."""

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

    async def open_dm(self, user_id: str) -> str | None:
        """The chat id of this bot's 1:1 with `user_id`, opening it if needed.

        `None` when the platform offers nowhere private to reach them — no DM
        surface at all, or an open that failed. Both leave a caller that needs
        privacy with no answer, which is the only distinction it can act on.
        Opening is idempotent wherever it is a call at all.
        """
        return None


class ChannelTentacle(
    Tentacle[RawT, MessageT],
):
    """Base class for IM channels.

    A channel receives native platform events, converts them to MessageEvents,
    and asks Octomate to dispatch the turn to the assigned agent. Outbound
    messages are encoded by Chromo and sent by the configured ink implementation.
    """

    FILES_ROOT: ClassVar[Path] = Path(".octomate/files")
    # How an inbound *threaded* message is routed, not what the bot can open:
    # `flat_thread` means a message carrying a thread_id continues that thread without
    # triage (reflex.Route). Vercel declares it and can open nothing — see `surfaces`.
    thread_strategy: ClassVar[ThreadStrategy] = "main_only"
    surfaces: ClassVar[ChannelSurfaces] = ChannelSurfaces()

    # The platform account this tentacle is logged in as (the bot's own
    # identity on the channel) — probe() fills it from ink.inspect().
    self_profile: UserProfile
    feelers: Feelers
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
        self.user_profiles: dict[str, UserProfile] = {}

        markdown_feeler = DefaultMarkdownFeeler(ink=self.ink, chromo=self.chromo)
        approvals_feeler = PlainTextApprovalFeeler(markdown_feeler)
        questions_feeler = PlainTextAskQuestionFeeler(markdown_feeler)
        oauth_feeler = PlainTextOAuthFeeler(self.ink, markdown_feeler)

        self.feelers = Feelers(
            markdown=markdown_feeler,
            timeline=DefaultTimelineFeeler[RawT, MessageT](
                ink=self.ink,
                chromo=self.chromo,
                ask_questions=questions_feeler,
                approvals=approvals_feeler,
                oauth=oauth_feeler,
                deferred_actions=self.octomate.deferred_actions,
            ),
            segments=DefaultSegmentsFeeler[RawT, MessageT](
                ink=self.ink, chromo=self.chromo
            ),
            approvals=approvals_feeler,
            ask_questions=questions_feeler,
            oauth=oauth_feeler,
        )

    def static_mounts(self) -> tuple[StaticMount, ...]:
        """Static-file surfaces the host app serves for this channel — empty by
        default. A channel that ships a web UI overrides this; `Octomate.app`
        mounts the declarations after every router, so they cannot shadow API
        routes."""
        return ()

    async def probe(self) -> None:
        """Resolve the channel's own identity from the platform. Awaited by the
        host before the channel is served, so `self.self_profile` is set before any
        inbound event is ingested."""
        self.self_profile = await self.ink.inspect()
        logger.info(
            "Channel %s: probed as %s (%s)",
            self.id,
            self.self_profile.channel_user_id,
            self.self_profile.name,
        )

    async def __aenter__(self) -> Self:
        # The ink first, so the probe already rides its pooled connection; then
        # resolve identity before any inbound event is ingested. Channels with a
        # persistent connection override this to open it (and `__aexit__` to tear
        # it down); HTTP-driven channels (e.g. the dev UI) need only the probe.
        await self.ink.__aenter__()
        await self.probe()
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.ink.__aexit__(*exc)

    @property
    def name(self) -> str:
        return self.self_profile.name

    @channel_logfire.instrument("ChannelTentacle {self.id} ingest")
    async def ingest(self, raw: RawT) -> None:
        """Inbound pipeline: decode, enrich sender, resolve media, dispatch."""
        address: ChannelAddress | None = None
        try:
            event = await self.chromo.sip(raw)
            if event is None:
                return
            event.tentacle_id = self.id
            event.self_id = self.self_profile.channel_user_id
            event.sender = await self.get_user_profile(event.user_id)
            await self.submerge(event)
            address = ChannelAddress(
                channel_tentacle_id=self.id,
                chat_type=event.chat_type,
                chat_id=event.chat_id,
                user_id=event.user_id,
                thread_id=event.thread_id,
            )
            channel_logfire.info(
                "ingest decoded message",
                channel_id=self.id,
                conversation_address=str(address),
                message_id=str(event.message_id),
            )
            thread_message = await self.octomate.thread_manager.record_inbound(event)
            if (
                self.config.mention_only
                and address.is_group
                and not event.is_at(self.self_profile.channel_user_id)
            ):
                logger.debug(
                    "Channel %s: ignored unmentioned group event %s",
                    self.id,
                    address,
                )
                return
            await self.octomate.kick(
                UserMessageSignal(
                    [event],
                    trigger_thread_message_id=thread_message.id,
                )
            )
        except Exception:
            # The active `ingest` span carries the full error; hand the user its
            # trace id so they can quote it and an operator can pull the detail
            # (e.g. a provider auth failure) from tracing.
            trace_id = format(
                trace.get_current_span().get_span_context().trace_id, "032x"
            )
            logger.exception(
                "Channel %s: error in ingest [trace_id=%s]", self.id, trace_id
            )
            if address is not None:
                try:
                    await self.feelers.markdown.present(
                        address,
                        "Something went wrong while handling your message. "
                        f"Reference id for tracing the issue: `{trace_id}`.",
                    )
                except Exception:
                    logger.warning(
                        "Channel %s: failed to deliver error notice",
                        self.id,
                        exc_info=True,
                    )

    async def open_dm(self, user_id: str) -> ChannelAddress | None:
        """The 1:1 conversation with `user_id`, opening it if the platform needs to.

        `None` when this channel has nowhere private to reach them; a channel opts
        in by declaring `surfaces.direct_message` and giving its ink an `open_dm`.
        The returned address may already carry a history, which is why a `teleport`
        there lands in a fresh thread or stays put rather than forking onto it.
        """
        if not user_id:
            return None
        chat_id = await self.ink.open_dm(user_id)
        if chat_id is None:
            return None
        return ChannelAddress(
            channel_tentacle_id=self.id,
            chat_type="private",
            chat_id=chat_id,
            user_id=user_id,
            thread_id="",
        )

    async def start_sub_thread(
        self,
        address: ChannelAddress,
        hint_text: str,
    ) -> ChannelAddress:
        logger.warning(
            "Channel %s does not support sub-thread startup; using main target",
            self.id,
        )
        await self.feelers.markdown.present(address, hint_text)
        return address

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
