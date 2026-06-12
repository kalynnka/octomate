"""The canonical fake channel.

`FakeChannelTentacle` is a real `ChannelTentacle` subclass: it runs the actual
ingest/consume/drive_timeline pipeline over a `RecordingInk`, so channel and
graph tests exercise the production code path and assert on what was actually
sent (`channel.ink.sent`) plus the consume/sub-thread call records.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator, AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import ClassVar, Generic, cast
from uuid import UUID

from typing_extensions import TypeVar

from pydantic_ai.result import FinalResult, StreamedRunResult
from typing_extensions import NotRequired, TypedDict

from octomate import Octomate
from octomate.capabilities.events import ResultSegmentEvent, ResultTextDeltaEvent
from octomate.capabilities.react import ReactStreamEvent
from octomate.config import ChannelConfig, ChannelStreamConfig
from octomate.managers.deferred import DeferredActionManager
from octomate.schemas.awakes import AwakeSignal
from octomate.schemas.conversation import ChatType, ConversationKey, UserProfile
from octomate.schemas.deferred import DeferredApproval, DeferredQuestion
from octomate.schemas.events import MessageEvent
from octomate.schemas.segments import ImageSegment, MessageSegment
from octomate.tentacles.channel.base import (
    ChannelOutput,
    ChannelTentacle,
    Chromo,
    DownloadedImage,
    Ink,
    ThreadStrategy,
)
from octomate.tentacles.channel.feelers.deferred import (
    ApprovalFeeler,
    QuestionFeeler,
)
from octomate.tentacles.channel.feelers.output import IMMessageID, TimelineState

NativeMessage = dict[str, str]


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
        default_factory=lambda: UserProfile(user_id="bot", name="Bot")
    )
    user_profiles: dict[str, UserProfile] = field(default_factory=dict)
    sent: list[tuple[str, str, list[NativeMessage], str | None, bool]] = field(
        default_factory=list
    )
    downloads: dict[str, DownloadedImage] = field(default_factory=dict)

    def inspect(self) -> UserProfile:
        return self.self_profile

    async def get_user_profile(self, user_id: str) -> UserProfile:
        return self.user_profiles.get(
            user_id,
            UserProfile(user_id=user_id, name=f"user-{user_id}"),
        )

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
        chat_type = raw.get("chat_type", "private")
        return MessageEvent(
            message_id=raw.get("message_id", "m1"),
            user_id=raw.get("user_id", "u1"),
            chat_id=raw.get("chat_id", "c1"),
            chat_type=chat_type,
            segments=raw.get("segments", []),
        )

    def outbound_markdown(self, text: str) -> list[NativeMessage]:
        return [{"text": text}] if text else []


class FakeChannelTentacle(ChannelTentacle[RawMessage, NativeMessage]):
    """Real channel pipeline over recording fakes. `start_sub_thread` succeeds
    and records, so the graph tests can route receptions into "hint-thread"."""

    thread_strategy: ClassVar[ThreadStrategy] = "flat_thread"

    recording_ink: RecordingInk
    sent: list[tuple[str, str, list[NativeMessage], str | None, bool]]
    consumed: list[tuple[ConversationKey, IMMessageID | None]]
    sub_threads: list[tuple[ConversationKey, str]]

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
                agent_id="inkling",
                stream=ChannelStreamConfig(),
            ),
        )
        self.sent = self.recording_ink.sent
        self.consumed = []
        self.sub_threads = []

    async def consume(
        self,
        key: ConversationKey,
        stream: AsyncIterator[
            ReactStreamEvent[ChannelOutput]
        ],
    ) -> IMMessageID | None:
        message_id = await super().consume(key, stream)
        self.consumed.append((key, message_id))
        return message_id

    async def start_sub_thread(
        self,
        key: ConversationKey,
        hint_text: str,
    ) -> ConversationKey:
        self.sub_threads.append((key, hint_text))
        return ConversationKey(
            channel_tentacle_id=key.channel_tentacle_id,
            chat_type=key.chat_type,
            chat_id=key.chat_id,
            user_id=key.user_id,
            thread_id="hint-thread",
        )


class MainOnlyChannelTentacle(FakeChannelTentacle):
    thread_strategy: ClassVar[ThreadStrategy] = "main_only"


class NoopTimeline(TimelineState):
    @asynccontextmanager
    async def open(self, key: ConversationKey) -> AsyncGenerator[NoopTimeline]:
        yield self


@dataclass
class RecordingTimeline(TimelineState):
    """Records every dispatched lifecycle call as a (method, payload) tuple."""

    calls: list[tuple[str, object]] = field(default_factory=list)
    message_id: IMMessageID | None = None

    @asynccontextmanager
    async def open(self, key: ConversationKey) -> AsyncGenerator[RecordingTimeline]:
        yield self

    async def thinking_start(self) -> None:
        self.calls.append(("thinking_start", None))

    async def thinking_delta(self, text: str) -> None:
        self.calls.append(("thinking_delta", text))

    async def thinking_end(self) -> None:
        self.calls.append(("thinking_end", None))

    async def answer_start(self) -> None:
        self.calls.append(("answer_start", None))

    async def answer_delta(self, text: str) -> None:
        self.calls.append(("answer_delta", text))

    async def answer_end(self) -> None:
        self.calls.append(("answer_end", None))

    async def answer_segment(self, segment: MessageSegment) -> None:
        self.calls.append(("answer_segment", segment))

    async def tool_start(self, event: object) -> None:
        self.calls.append(("tool_start", event))

    async def tool_end(self, event: object) -> None:
        self.calls.append(("tool_end", event))

    async def todo(self, event: object) -> None:
        self.calls.append(("todo", event))

    def names(self) -> list[str]:
        return [name for name, _ in self.calls]


@dataclass
class RecordingMarkdownFeeler:
    calls: list[tuple[ConversationKey, str]] = field(default_factory=list)

    async def present(
        self,
        key: ConversationKey,
        markdown: str,
    ) -> IMMessageID | None:
        self.calls.append((key, markdown))
        return "markdown-message"


class NoopMarkdownStreamFeeler:
    async def present(
        self,
        key: ConversationKey,
        stream: StreamedRunResult[None, ChannelOutput],
    ) -> IMMessageID | None:
        return None

    async def present_output(
        self,
        key: ConversationKey,
        events: AsyncIterator[
            ResultTextDeltaEvent | ResultSegmentEvent | FinalResult[ChannelOutput]
        ],
    ) -> IMMessageID | None:
        return None


@dataclass
class RecordingApprovalFeeler(ApprovalFeeler):
    presented: list[tuple[ConversationKey, list[DeferredApproval]]] = field(
        default_factory=list
    )

    async def present(
        self,
        key: ConversationKey,
        actions: list[DeferredApproval],
    ) -> dict[UUID, IMMessageID | None]:
        self.presented.append((key, list(actions)))
        return {action.id: f"approval-{action.id}" for action in actions}


@dataclass
class RecordingQuestionFeeler(QuestionFeeler):
    presented: list[tuple[ConversationKey, list[DeferredQuestion]]] = field(
        default_factory=list
    )

    async def present(
        self,
        key: ConversationKey,
        actions: list[DeferredQuestion],
    ) -> dict[UUID, IMMessageID | None]:
        self.presented.append((key, list(actions)))
        return {action.id: f"question-{action.id}" for action in actions}


StreamOutputT = TypeVar("StreamOutputT", bound=ChannelOutput)


@dataclass
class FakeStreamedRunResult(Generic[StreamOutputT]):
    output: StreamOutputT
    text_deltas: list[str] = field(default_factory=list)
    fail_text_stream: bool = False

    async def stream_text(
        self,
        *,
        delta: bool = False,
        debounce_by: float | None = 0.1,
    ) -> AsyncIterator[str]:
        if self.fail_text_stream or not isinstance(self.output, str):
            raise RuntimeError("text stream unavailable")
        for text in self.text_deltas or [self.output]:
            yield text

    async def stream_output(
        self,
        *,
        debounce_by: float | None = 0.1,
    ) -> AsyncIterator[StreamOutputT]:
        if self.fail_text_stream:
            raise RuntimeError("output stream unavailable")
        if isinstance(self.output, str):
            content = ""
            for text in self.text_deltas or [self.output]:
                content += text
                yield cast(StreamOutputT, content)
            return
        yield self.output

    async def get_output(self) -> StreamOutputT:
        return self.output


def streamed_result(
    output: StreamOutputT,
    *text_deltas: str,
    fail_text_stream: bool = False,
) -> StreamedRunResult[None, StreamOutputT]:
    return cast(
        StreamedRunResult[None, StreamOutputT],
        FakeStreamedRunResult(
            output=output,
            text_deltas=list(text_deltas),
            fail_text_stream=fail_text_stream,
        ),
    )


async def output_events(
    *deltas: str,
) -> AsyncIterator[ResultTextDeltaEvent | FinalResult[ChannelOutput]]:
    """A streamed text reply as the typed output event stream a feeler's
    `present_output` consumes: each chunk as an additive `ResultTextDeltaEvent`
    (Pydantic AI's typewriter), then the joined whole as `FinalResult`."""
    for delta in deltas:
        yield ResultTextDeltaEvent(delta=delta)
    if deltas:
        yield FinalResult[ChannelOutput](output="".join(deltas))
