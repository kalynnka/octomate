"""Shared channel primitives.

Type variables:
- RawT: native inbound platform payload accepted by a channel and its chromo.
- MessageT: native outbound platform message payload built and sent by an ink.
"""

from __future__ import annotations

import asyncio
import logging
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, ClassVar, Generic, Literal, TypeAlias, TypeVar, cast

import anyio
import logfire
from pydantic_ai import AgentRunResultEvent
from pydantic_ai.messages import (
    FunctionToolCallEvent,
    FunctionToolResultEvent,
    OutputToolCallEvent,
    OutputToolResultEvent,
    PartDeltaEvent,
    PartEndEvent,
    PartStartEvent,
    TextPart,
    TextPartDelta,
    ThinkingPart,
    ThinkingPartDelta,
)
from pydantic_ai.tools import DeferredToolRequests
from uuid_utils import uuid7

from octomate.capabilities.events import (
    ActionBatchEvent,
    ResultSegmentEvent,
    StreamEvents,
    TodoCompletedEvent,
    TodoCreatedEvent,
    TodoDeletedEvent,
    TodoStatusChangedEvent,
    TodoUpdatedEvent,
)
from octomate.config import ChannelConfig
from octomate.schemas.awakes import UserMessageSignal
from octomate.schemas.conversation import ConversationKey, UserProfile
from octomate.schemas.events import MessageEvent
from octomate.schemas.segments import ImageSegment, MessageSegment, Segment
from octomate.tentacles.base import Tentacle
from octomate.tentacles.channel.feelers.base import Feelers
from octomate.tentacles.channel.feelers.deferred import (
    PlainTextApprovalFeeler,
    PlainTextAskQuestionFeeler,
)
from octomate.tentacles.channel.feelers.output import (
    DefaultMarkdownFeeler,
    DefaultMarkdownStreamFeeler,
    DefaultTimelineFeeler,
    IMMessageID,
    TimelineState,
    should_skip_plan_tool,
)
from octomate.utils import guess_image_ext

if TYPE_CHECKING:
    from octomate.base import Octomate

logger = logging.getLogger(__name__)

ThreadStrategy = Literal["main_only", "flat_thread", "nested_thread"]
ChannelOutput: TypeAlias = str | Sequence[MessageSegment] | DeferredToolRequests | None
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
            timeline=DefaultTimelineFeeler[RawT, MessageT](
                ink=self.ink, chromo=self.chromo
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

    async def consume(
        self,
        key: ConversationKey,
        stream: AsyncIterator[
            StreamEvents[ChannelOutput] | AgentRunResultEvent[ChannelOutput]
        ],
    ) -> IMMessageID | None:
        """Drive a typed run stream to the channel's timeline renderer.

        The per-run timeline is an async context manager: entering it acquires the
        platform resource (e.g. a stream), `drive_timeline` renders each event, and
        exiting releases the resource and sets `message_id`.
        """
        async with self.feelers.timeline.open(key) as state:
            await self.drive_timeline(key, stream, state)
        return state.message_id

    async def drive_timeline(
        self,
        key: ConversationKey,
        stream: AsyncIterator[
            StreamEvents[ChannelOutput] | AgentRunResultEvent[ChannelOutput]
        ],
        state: TimelineState,
    ) -> None:
        """The single event→render dispatch over the run stream; each typed event is
        drawn onto `state` — the per-run timeline the channel's feeler opened, which
        renders itself. Draining the whole stream is mandatory — abandoning it mid-run
        tears down the agent task group from the wrong task.
        """
        skipped_tools: set[str] = set()
        failed = False
        answered = False
        final_output: ChannelOutput = None
        async for event in stream:
            if failed:
                continue  # keep draining the stream even after a render failure
            try:
                match event:
                    case PartStartEvent(part=ThinkingPart(content=content)):
                        await state.thinking_start()
                        if content:
                            await state.thinking_delta(content)
                    case PartDeltaEvent(delta=ThinkingPartDelta(content_delta=delta)):
                        await state.thinking_delta(delta or "")
                    case PartEndEvent(part=ThinkingPart()):
                        await state.thinking_end()
                    case PartStartEvent(part=TextPart(content=content)):
                        answered = True
                        await state.answer_start()
                        if content:
                            await state.answer_delta(content)
                    case PartDeltaEvent(delta=TextPartDelta(content_delta=delta)):
                        answered = answered or bool(delta)
                        await state.answer_delta(delta or "")
                    case PartEndEvent(part=TextPart()):
                        await state.answer_end()
                    case FunctionToolCallEvent() | OutputToolCallEvent():
                        if should_skip_plan_tool(event.part.tool_name):
                            if event.part.tool_call_id:
                                skipped_tools.add(event.part.tool_call_id)
                        else:
                            await state.tool_start(event)
                    case FunctionToolResultEvent() | OutputToolResultEvent():
                        part = event.part
                        tool_call_id = getattr(part, "tool_call_id", None)
                        if should_skip_plan_tool(part.tool_name or "") or (
                            tool_call_id is not None and tool_call_id in skipped_tools
                        ):
                            skipped_tools.discard(tool_call_id or "")
                        else:
                            await state.tool_end(event)
                    case ResultSegmentEvent():
                        answered = True
                        await state.answer_segment(event.segment)
                    case (
                        TodoCreatedEvent()
                        | TodoUpdatedEvent()
                        | TodoStatusChangedEvent()
                        | TodoCompletedEvent()
                        | TodoDeletedEvent()
                    ):
                        await state.todo(event)
                    case ActionBatchEvent():
                        # Render the deferred-action batch as a unit, then record
                        # each presented action's platform message id.
                        answered = answered or bool(event.questions or event.approvals)
                        if event.questions:
                            message_ids = await self.feelers.ask_questions.present(
                                key, event.questions
                            )
                            for action in event.questions:
                                await self.octomate.deferred_actions.mark_action_presented(
                                    action.id, message_ids.get(action.id)
                                )
                        if event.approvals:
                            message_ids = await self.feelers.approvals.present(
                                key, event.approvals
                            )
                            for action in event.approvals:
                                await self.octomate.deferred_actions.mark_action_presented(
                                    action.id, message_ids.get(action.id)
                                )
                    case AgentRunResultEvent():
                        final_output = event.result.output  # for the fallback below
                    case _:
                        pass  # FinalResult / PartEnd of other parts / passthrough
            except Exception:
                logger.warning(
                    "Channel %s: timeline render failed", self.id, exc_info=True
                )
                failed = True
        if (
            not failed
            and not answered
            and isinstance(final_output, Sequence)
            and all(isinstance(segment, Segment) for segment in final_output)
        ):
            for segment in final_output:
                await state.answer_segment(cast(MessageSegment, segment))
        elif not failed and not answered and final_output is not None:
            # The reply never streamed; render the final output once so the turn
            # is not left blank.
            await state.answer_delta(str(final_output))

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
