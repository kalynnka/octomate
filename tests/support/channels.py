"""The canonical fake channel.

`FakeChannelTentacle` is a real `ChannelTentacle` subclass: it runs the actual
ingest pipeline over a `RecordingInk`, so channel and graph tests exercise the
production code path and assert on what was actually sent (`channel.ink.sent`)
plus the consume/sub-thread call records. `drive` mirrors the production
inline-consume path (open the timeline feeler, `state.drive` the stream); `bound`
binds a bare `TimelineState` to a channel's feelers for the direct-drive tests.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator, AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any, ClassVar, NotRequired
from uuid import UUID

from pydantic_ai.messages import (
    FunctionToolCallEvent,
    FunctionToolResultEvent,
    OutputToolCallEvent,
    OutputToolResultEvent,
)
from typing_extensions import TypedDict

from octomate import Octomate
from octomate.capabilities.harness.events import (
    SubagentActivity,
    SubagentActivityStatus,
    TodoEvent,
)
from octomate.capabilities.harness.react import ReactStreamEvent
from octomate.config import ChannelConfig, ChannelStreamConfig
from octomate.managers.deferred import DeferredActionManager
from octomate.schemas.awakes import AwakeSignal
from octomate.schemas.conversation import ChannelAddress, ChatType
from octomate.schemas.deferred import DeferredApproval, DeferredQuestion
from octomate.schemas.events import MessageEvent
from octomate.schemas.segments import ImageSegment, MessageSegment
from octomate.schemas.user import UserProfile
from octomate.tentacles.channels.base import (
    ChannelOutput,
    ChannelSurfaces,
    ChannelTentacle,
    Chromo,
    DownloadedImage,
    Ink,
    ThreadStrategy,
)
from octomate.tentacles.channels.feelers.deferred import (
    ApprovalFeeler,
    QuestionFeeler,
)
from octomate.tentacles.channels.feelers.output import (
    IMMessageID,
    SubagentTimelineState,
    TimelineFeeler,
    TimelineState,
)

NativeMessage = dict[str, str]


async def drive(
    channel: ChannelTentacle[Any, Any],
    address: ChannelAddress,
    stream: AsyncIterator[ReactStreamEvent[ChannelOutput]],
) -> IMMessageID | None:
    """The production inline-consume path: open the timeline feeler and drive the
    run stream onto the per-run state."""
    async with channel.feelers.timeline.open(address) as state:
        await state.drive(stream)
    return state.message_id


def bound(
    state: TimelineState,
    channel: ChannelTentacle[Any, Any],
    address: ChannelAddress,
) -> TimelineState:
    """Inject a channel's deferred-action feelers into a bare `TimelineState`, the
    way the timeline feeler's `open()` would, so `state.drive` can run standalone."""
    state.address = address
    state.ask_questions = channel.feelers.ask_questions
    state.approvals = channel.feelers.approvals
    state.deferred_actions = channel.octomate.deferred_actions
    return state


class RawMessage(TypedDict, total=False):
    message_id: str
    user_id: str
    chat_id: str
    chat_type: ChatType
    segments: NotRequired[list[MessageSegment]]


@dataclass
class FakeOctomate(Octomate):
    kicks: list[AwakeSignal] = field(default_factory=list)

    async def kick(
        self,
        signal: AwakeSignal,
    ) -> None:
        self.kicks.append(signal)


@dataclass
class RecordingDeferredActions(DeferredActionManager):
    marked: list[tuple[UUID, str | None]] = field(default_factory=list)

    async def mark_action_presented(
        self,
        action_id: UUID,
        platform_message_id: str | None,
    ) -> None:
        self.marked.append((action_id, platform_message_id))


@dataclass
class RecordingInk(Ink[NativeMessage]):
    self_profile: UserProfile = field(
        default_factory=lambda: UserProfile(channel_user_id="bot", name="Bot")
    )
    user_profiles: dict[str, UserProfile] = field(default_factory=dict)
    sent: list[tuple[str, str, list[NativeMessage], str | None, bool]] = field(
        default_factory=list
    )
    downloads: dict[str, DownloadedImage] = field(default_factory=dict)
    opened_dms: list[str] = field(default_factory=list)
    # Whether this platform will hand back a private chat; False is the open
    # failing at the moment of asking.
    dm_opens: bool = True

    async def inspect(self) -> UserProfile:
        return self.self_profile

    async def get_user_profile(self, user_id: str) -> UserProfile:
        return self.user_profiles.get(
            user_id,
            UserProfile(channel_user_id=user_id, name=f"user-{user_id}"),
        )

    async def open_dm(self, user_id: str) -> str | None:
        # A user's own id is their private chat id, as on Lark and NapCat — and what
        # an inbound private message decodes to, so a DM seeded by one is the same
        # thread the channel's `open_dm` builds.
        self.opened_dms.append(user_id)
        return user_id if self.dm_opens else None

    async def upload_media(self, data: bytes) -> str | None:
        return None

    async def download_image(
        self,
        seg: ImageSegment,
        message_id: str,
    ) -> DownloadedImage | None:
        return self.downloads.get(seg.data.file)

    async def send_message(
        self,
        chat_id: str,
        chat_type: str,
        messages: list[NativeMessage],
        reply_to: str | None = None,
        reply_in_thread: bool = False,
    ) -> str | None:
        self.sent.append((chat_id, chat_type, messages, reply_to, reply_in_thread))
        return f"sent-{len(self.sent)}"


@dataclass
class FakeChromo(Chromo[RawMessage, NativeMessage]):
    sip_calls: list[RawMessage] = field(default_factory=list)

    async def sip(self, raw: RawMessage) -> MessageEvent | None:
        self.sip_calls.append(raw)
        chat_type = raw.get("chat_type", "dm")
        return MessageEvent(
            message_id=raw.get("message_id", "m1"),
            user_id=raw.get("user_id", "u1"),
            chat_id=raw.get("chat_id", "c1"),
            chat_type=chat_type,
            segments=raw.get("segments", []),
        )

    def outbound_markdown(self, text: str) -> list[NativeMessage]:
        return [{"text": text}] if text else []


@dataclass
class RecordingTimelineFeeler:
    """Wraps a channel's timeline feeler to log every `open()`'d run as a
    `(address, message_id)` pair, so graph tests can assert a reception streamed
    through the timeline (the inline-consume path) and to which conversation."""

    inner: TimelineFeeler
    consumed: list[tuple[ChannelAddress, IMMessageID | None]]

    @asynccontextmanager
    async def open(self, address: ChannelAddress) -> AsyncGenerator[TimelineState]:
        async with self.inner.open(address) as state:
            yield state
        self.consumed.append((address, state.message_id))


@dataclass
class RecordingSubagentTimelineState(SubagentTimelineState):
    address: ChannelAddress
    activity: SubagentActivity
    response: str = ""
    settlements: list[tuple[SubagentActivityStatus, str | None]] = field(
        default_factory=list
    )
    closed: bool = False
    fail_updates: bool = False

    async def append_response(self, delta: str) -> None:
        if self.fail_updates:
            raise RuntimeError("subagent timeline update failed")
        self.response += delta

    async def settle(
        self,
        status: SubagentActivityStatus,
        detail: str | None = None,
    ) -> None:
        if self.fail_updates:
            raise RuntimeError("subagent timeline update failed")
        self.settlements.append((status, detail))


class FakeChannelTentacle(ChannelTentacle[RawMessage, NativeMessage]):
    """Real channel pipeline over recording fakes. `start_sub_thread` succeeds
    and records, so the graph tests can route receptions into "hint-thread".
    `consume` mirrors the production inline path (open the timeline, drive the
    stream); the recording timeline feeler logs each run into `consumed`."""

    thread_strategy: ClassVar[ThreadStrategy] = "flat_thread"
    surfaces: ClassVar[ChannelSurfaces] = ChannelSurfaces(
        sub_thread=True, direct_message=True
    )

    recording_ink: RecordingInk
    sent: list[tuple[str, str, list[NativeMessage], str | None, bool]]
    consumed: list[tuple[ChannelAddress, IMMessageID | None]]
    sub_threads: list[tuple[ChannelAddress, str]]
    opened_dms: list[str]

    def __init__(
        self,
        id: str = "im",
        octomate: Octomate | None = None,
        *,
        ink: RecordingInk | None = None,
        chromo: FakeChromo | None = None,
        config: ChannelConfig | None = None,
    ) -> None:
        self.recording_ink = ink or RecordingInk()
        super().__init__(
            id=id,
            octomate=octomate or FakeOctomate(),
            ink=self.recording_ink,
            chromo=chromo or FakeChromo(),
            config=config
            or ChannelConfig(
                type="fake",
                stream=ChannelStreamConfig(),
            ),
        )
        self.sent = self.recording_ink.sent
        self.consumed = []
        self.sub_threads = []
        self.opened_dms = self.recording_ink.opened_dms
        self.self_profile = self.recording_ink.self_profile
        self.feelers.timeline = RecordingTimelineFeeler(
            self.feelers.timeline, self.consumed
        )

    async def consume(
        self,
        address: ChannelAddress,
        stream: AsyncIterator[ReactStreamEvent[ChannelOutput]],
    ) -> IMMessageID | None:
        return await drive(self, address, stream)

    async def start_sub_thread(
        self,
        address: ChannelAddress,
        hint_text: str,
    ) -> ChannelAddress:
        self.sub_threads.append((address, hint_text))
        return ChannelAddress(
            channel_tentacle_id=address.channel_tentacle_id,
            chat_type=address.chat_type,
            chat_id=address.chat_id,
            user_id=address.user_id,
            channel_thread_id="hint-thread",
        )


class MainOnlyChannelTentacle(FakeChannelTentacle):
    thread_strategy: ClassVar[ThreadStrategy] = "main_only"
    surfaces: ClassVar[ChannelSurfaces] = ChannelSurfaces(direct_message=True)


class NoopTimeline(TimelineState):
    @asynccontextmanager
    async def open(self, address: ChannelAddress) -> AsyncGenerator[NoopTimeline]:
        yield self


@dataclass
class RecordingTimeline(TimelineState):
    """Records every dispatched lifecycle call as a (method, payload) tuple,
    honoring the mid-run-notice contract (`begin_entry`/`noticed`) so rotation
    dispatch is observable too."""

    calls: list[tuple[str, object]] = field(default_factory=list)
    subagent_states: list[RecordingSubagentTimelineState] = field(default_factory=list)
    fail_subagent_open: bool = False
    fail_subagent_updates: bool = False
    message_id: IMMessageID | None = None

    @asynccontextmanager
    async def open(self, address: ChannelAddress) -> AsyncGenerator[RecordingTimeline]:
        self.address = address
        try:
            yield self
        except asyncio.CancelledError:
            await self.settle_subagents("cancelled")
            raise
        finally:
            await self.settle_subagents("failed")

    @asynccontextmanager
    async def open_subagent(
        self,
        activity: SubagentActivity,
    ) -> AsyncGenerator[RecordingSubagentTimelineState]:
        if self.fail_subagent_open:
            raise RuntimeError("subagent timeline open failed")
        state = RecordingSubagentTimelineState(
            self.address,
            activity,
            fail_updates=self.fail_subagent_updates,
        )
        self.subagent_states.append(state)
        try:
            yield state
        finally:
            state.closed = True

    async def thinking_start(self) -> None:
        await self.begin_entry()
        self.calls.append(("thinking_start", None))

    async def thinking_delta(self, text: str) -> None:
        self.calls.append(("thinking_delta", text))

    async def thinking_end(self) -> None:
        self.calls.append(("thinking_end", None))

    async def answer_start(self) -> None:
        self.calls.append(("answer_start", None))

    async def answer_delta(self, text: str) -> None:
        self.noticed = True
        self.calls.append(("answer_delta", text))

    async def answer_end(self) -> None:
        self.calls.append(("answer_end", None))

    async def answer_segment(self, segment: MessageSegment) -> None:
        self.calls.append(("answer_segment", segment))

    async def tool_start(
        self, event: FunctionToolCallEvent | OutputToolCallEvent
    ) -> None:
        await self.begin_entry()
        self.calls.append(("tool_start", event))

    async def tool_end(
        self, event: FunctionToolResultEvent | OutputToolResultEvent
    ) -> None:
        self.calls.append(("tool_end", event))

    async def todo(self, event: TodoEvent) -> None:
        await self.begin_entry()
        self.calls.append(("todo", event))

    async def rotate(self) -> None:
        self.calls.append(("rotate", None))

    def names(self) -> list[str]:
        return [name for name, _ in self.calls]


@dataclass
class RecordingMarkdownFeeler:
    calls: list[tuple[ChannelAddress, str]] = field(default_factory=list)

    async def present(
        self,
        address: ChannelAddress,
        markdown: str,
    ) -> IMMessageID | None:
        self.calls.append((address, markdown))
        return "markdown-message"


class NoopSegmentsFeeler:
    async def present(
        self,
        address: ChannelAddress,
        segments: list[MessageSegment],
    ) -> IMMessageID | None:
        return None


@dataclass
class RecordingApprovalFeeler(ApprovalFeeler):
    presented: list[tuple[ChannelAddress, list[DeferredApproval]]] = field(
        default_factory=list
    )

    async def present(
        self,
        address: ChannelAddress,
        actions: list[DeferredApproval],
    ) -> dict[UUID, IMMessageID | None]:
        self.presented.append((address, list(actions)))
        return {action.id: f"approval-{action.id}" for action in actions}


@dataclass
class RecordingQuestionFeeler(QuestionFeeler):
    presented: list[tuple[ChannelAddress, list[DeferredQuestion]]] = field(
        default_factory=list
    )

    async def present(
        self,
        address: ChannelAddress,
        actions: list[DeferredQuestion],
    ) -> dict[UUID, IMMessageID | None]:
        self.presented.append((address, list(actions)))
        return {action.id: f"question-{action.id}" for action in actions}


@dataclass
class FakeOAuthInk:
    """The ink an `OAuthFeeler` asks for somewhere private to put a one-time code.

    `dm_chat_id` is what `open_dm` answers, so a test says whether this platform
    has one; `opened` records who it was asked for.
    """

    dm_chat_id: str | None = None
    opened: list[str] = field(default_factory=list)

    async def open_dm(self, user_id: str) -> str | None:
        self.opened.append(user_id)
        return self.dm_chat_id
