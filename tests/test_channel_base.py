from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from uuid import UUID

from typing_extensions import NotRequired, TypedDict

import pytest
from pydantic_ai import AgentRunResult, AgentRunResultEvent
from pydantic_ai.result import FinalResult
from pydantic_ai.messages import (
    FunctionToolCallEvent,
    FunctionToolResultEvent,
    PartDeltaEvent,
    PartStartEvent,
    TextPart,
    TextPartDelta,
    ThinkingPart,
    ThinkingPartDelta,
    ToolCallPart,
    ToolReturnPart,
)
from pydantic_ai.result import StreamedRunResult

from octomate import Octomate
from octomate.config import ChannelConfig, ChannelStreamConfig
from octomate.managers.conversations import ConversationManager
from octomate.managers.deferred import DeferredActionManager
from octomate.schemas.awakes import AwakeSignal, UserMessageSignal
from octomate.schemas.conversation import ChatType, ConversationKey, UserProfile
from octomate.schemas.deferred import (
    ApprovalRequest,
    DeferredApproval,
    DeferredQuestion,
)
from octomate.schemas.events import MessageEvent
from octomate.schemas.segments import (
    AtData,
    AtSegment,
    ImageSegment,
    MessageSegment,
    TextSegment,
)
from octomate.capabilities.events import (
    ActionBatchEvent,
    ResultTextDeltaEvent,
    StreamEvents,
)
from octomate.tentacles.channel.base import (
    ChannelOutput,
    ChannelTentacle,
    Chromo,
    DownloadedImage,
    Ink,
)
from octomate.tentacles.channel.feelers.output import (
    StreamBlock,
    TextStreamBatcher,
    render_stream_event_delta,
)

NativeMessage = dict[str, str]


class RawMessage(TypedDict, total=False):
    message_id: str
    user_id: str
    chat_id: str
    chat_type: ChatType
    segments: NotRequired[list[MessageSegment]]


@dataclass
class FakeOctomate(Octomate):
    conversations: ConversationManager = field(default_factory=ConversationManager)
    kicks: list[AwakeSignal] = field(default_factory=list)

    async def kick(
        self,
        signal: AwakeSignal,
    ) -> None:
        self.kicks.append(signal)


@dataclass
class FakeDeferredActions(DeferredActionManager):
    marked: list[tuple[UUID, str | None]] = field(default_factory=list)

    async def mark_action_presented(
        self,
        action_id: UUID,
        platform_message_id: str | None,
    ) -> None:
        self.marked.append((action_id, platform_message_id))


@dataclass
class FakeInk(Ink[NativeMessage]):
    self_profile: UserProfile = field(
        default_factory=lambda: UserProfile(user_id="bot", name="Bot")
    )
    user_profiles: dict[str, UserProfile] = field(default_factory=dict)
    sent: list[tuple[str, str, list[NativeMessage], str | None, bool]] = field(
        default_factory=list
    )

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
        return None

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
    sent: list[tuple[str, str, list[NativeMessage], str | None, bool]]

    def __init__(
        self,
        id: str,
        octomate: FakeOctomate,
        ink: FakeInk,
        chromo: FakeChromo,
        *,
        mention_only: bool = True,
    ) -> None:
        super().__init__(
            id=id,
            octomate=octomate,
            ink=ink,
            chromo=chromo,
            config=ChannelConfig(
                type="fake",
                agent_id="inkling",
                mention_only=mention_only,
                stream=ChannelStreamConfig(),
            ),
        )
        self.sent = ink.sent


@pytest.fixture
def channel() -> FakeChannelTentacle:
    return FakeChannelTentacle(
        id="chan1",
        octomate=FakeOctomate(),
        ink=FakeInk(),
        chromo=FakeChromo(),
    )


async def test_ingest_dispatches_event_to_octomate(
    channel: FakeChannelTentacle,
) -> None:
    raw: RawMessage = {
        "message_id": "m42",
        "user_id": "alice",
        "chat_id": "lobby",
        "chat_type": "group",
        "segments": [
            AtSegment(data=AtData(user_id="bot")),
            TextSegment(data={"text": "hello"}),
        ],
    }

    await channel.ingest(raw)

    octomate = channel.octomate
    assert isinstance(octomate, FakeOctomate)
    assert len(octomate.kicks) == 1
    signal = octomate.kicks[0]
    assert isinstance(signal, UserMessageSignal)
    key = signal.key

    assert key.channel_tentacle_id == "chan1"
    assert key.chat_id == "lobby"
    assert key.chat_type == "group"
    assert key.user_id == "alice"

    event = signal.messages[0]
    assert event.tentacle_id == "chan1"
    assert event.self_id == "bot"
    assert event.sender.user_id == "alice"


async def test_group_mention_filter_ignores_unmentioned_events() -> None:
    channel = FakeChannelTentacle(
        id="chan1",
        octomate=FakeOctomate(),
        ink=FakeInk(),
        chromo=FakeChromo(),
        mention_only=True,
    )
    await channel.ingest(
        {
            "message_id": "m42",
            "user_id": "alice",
            "chat_id": "lobby",
            "chat_type": "group",
            "segments": [TextSegment(data={"text": "hello"})],
        }
    )

    octomate = channel.octomate
    assert isinstance(octomate, FakeOctomate)
    assert octomate.kicks == []


async def test_markdown_feeler_encodes_final_agent_result_and_sends_native_messages(
    channel: FakeChannelTentacle,
) -> None:
    key = ConversationKey(
        channel_tentacle_id="chan1",
        chat_type="private",
        chat_id="alice",
        user_id="alice",
    )

    await channel.feelers.markdown.present(key, "hi alice")

    assert len(channel.sent) == 1
    chat_id, chat_type, messages, reply_to, _ = channel.sent[0]
    assert chat_id == "alice"
    assert chat_type == "private"
    assert messages[0]["text"] == "hi alice"
    assert reply_to is None


async def test_markdown_feeler_uses_conversation_thread_as_reply_target(
    channel: FakeChannelTentacle,
) -> None:
    key = ConversationKey(
        channel_tentacle_id="chan1",
        chat_type="group",
        chat_id="lobby",
        user_id="alice",
        thread_id="m1",
    )

    await channel.feelers.markdown.present(key, "after reply")

    assert len(channel.sent) == 1
    assert channel.sent[0][3] == "m1"
    assert channel.sent[0][2][0]["text"] == "after reply"


async def test_markdown_stream_feeler_default_sends_final_result(
    channel: FakeChannelTentacle,
    caplog: pytest.LogCaptureFixture,
) -> None:
    key = ConversationKey(
        channel_tentacle_id="chan1",
        chat_type="private",
        chat_id="alice",
        user_id="alice",
    )

    with caplog.at_level("WARNING"):
        await channel.feelers.markdown_stream.present(
            key,
            StreamedRunResult([], 0, run_result=AgentRunResult("stream me")),
        )

    assert "does not support streaming responses" not in caplog.text
    assert len(channel.sent) == 1
    assert channel.sent[0][2][0]["text"] == "stream me"


async def test_markdown_stream_feeler_present_output_sends_final(
    channel: FakeChannelTentacle,
    caplog: pytest.LogCaptureFixture,
) -> None:
    key = ConversationKey(
        channel_tentacle_id="chan1",
        chat_type="private",
        chat_id="alice",
        user_id="alice",
    )

    async def events() -> (
        AsyncIterator[ResultTextDeltaEvent | FinalResult[ChannelOutput]]
    ):
        yield ResultTextDeltaEvent(delta="strea")
        yield FinalResult[ChannelOutput](output="stream me")

    with caplog.at_level("WARNING"):
        await channel.feelers.markdown_stream.present_output(key, events())

    assert len(channel.sent) == 1
    assert channel.sent[0][2][0]["text"] == "stream me"
    # The Default stream feeler has no streaming transport, so it warns and sends
    # the reply as a single message.
    assert "no streaming transport" in caplog.text


async def test_consume_renders_answer_and_drains_stream(
    channel: FakeChannelTentacle,
    caplog: pytest.LogCaptureFixture,
) -> None:
    key = ConversationKey(
        channel_tentacle_id="chan1",
        chat_type="private",
        chat_id="alice",
        user_id="alice",
    )
    drained = False

    async def events() -> (
        AsyncIterator[StreamEvents[ChannelOutput] | AgentRunResultEvent[ChannelOutput]]
    ):
        nonlocal drained
        # Thinking + tool passthrough exercise the dispatch; the Default timeline
        # (no streaming transport) drops them and only accumulates the answer text.
        yield PartStartEvent(index=0, part=ThinkingPart(content="hmm"))
        yield FunctionToolCallEvent(
            part=ToolCallPart(tool_name="search", args={"q": "x"}, tool_call_id="t1")
        )
        yield ResultTextDeltaEvent(delta="stream ")
        yield ResultTextDeltaEvent(delta="me")
        yield FinalResult[ChannelOutput](output="stream me")
        # Reached only if consume() drains the whole stream past FinalResult.
        drained = True

    with caplog.at_level("WARNING"):
        message_id = await channel.consume(key, events())

    assert message_id is not None
    assert len(channel.sent) == 1
    assert channel.sent[0][2][0]["text"] == "stream me"
    assert "no streaming transport" in caplog.text
    assert drained


async def test_consume_renders_and_marks_action_batch(
    channel: FakeChannelTentacle,
) -> None:
    key = ConversationKey(
        channel_tentacle_id="chan1",
        chat_type="private",
        chat_id="alice",
        user_id="alice",
    )
    question = DeferredQuestion(
        tool_name="ask_questions",
        tool_call_id="c1",
        args={"question": "Pick one?"},
    )
    approval = DeferredApproval(
        tool_name="do_thing",
        tool_call_id="c2",
        args=ApprovalRequest(tool_name="do_thing"),
    )
    actions = FakeDeferredActions()
    channel.octomate.deferred_actions = actions

    async def events() -> (
        AsyncIterator[StreamEvents[ChannelOutput] | AgentRunResultEvent[ChannelOutput]]
    ):
        yield ActionBatchEvent(
            batch_id="b1", questions=[question], approvals=[approval]
        )

    await channel.consume(key, events())

    # The batch renders through the channel's (plaintext) question/approval feelers
    # as a unit, and each presented action is marked with its message id.
    assert len(channel.sent) == 2
    assert "Pick one?" in channel.sent[0][2][0]["text"]
    assert "do_thing" in channel.sent[1][2][0]["text"]
    marked = {str(action_id): message_id for action_id, message_id in actions.marked}
    assert marked[str(question.id)] == "sent-1"
    assert marked[str(approval.id)] == "sent-2"


async def test_markdown_feeler_uses_normal_adapter(
    channel: FakeChannelTentacle,
) -> None:
    key = ConversationKey(
        channel_tentacle_id="chan1",
        chat_type="private",
        chat_id="alice",
        user_id="alice",
    )

    await channel.feelers.markdown.present(key, "fallback")

    assert len(channel.sent) == 1
    assert channel.sent[0][2][0]["text"] == "fallback"


async def test_start_sub_thread_falls_back_to_main_target(
    channel: FakeChannelTentacle,
    caplog: pytest.LogCaptureFixture,
) -> None:
    key = ConversationKey(
        channel_tentacle_id="chan1",
        chat_type="private",
        chat_id="alice",
        user_id="alice",
    )

    with caplog.at_level("WARNING"):
        returned = await channel.start_sub_thread(key, "handoff")

    assert returned == key
    assert "does not support sub-thread startup" in caplog.text
    assert len(channel.sent) == 1
    assert channel.sent[0][2][0]["text"] == "handoff"


def test_text_stream_batcher_immediate_mode_flushes_each_push() -> None:
    batcher = TextStreamBatcher(flush_interval=0)

    first = batcher.push_text("hel")
    second = batcher.push_text("lo")

    assert [update.delta_text for update in first + second] == ["hel", "lo"]
    assert [update.sequence for update in first + second] == [1, 2]


def test_text_stream_batcher_flushes_by_time_and_size() -> None:
    now = 0.0

    def clock() -> float:
        return now

    batcher = TextStreamBatcher(
        flush_interval=0.2,
        min_chars=3,
        max_chars=10,
        clock=clock,
    )

    assert batcher.push_text("he") == []
    now = 0.3
    updates = batcher.push_text("y")

    assert len(updates) == 1
    assert updates[0].delta_text == "hey"
    assert updates[0].full_text == "hey"

    updates = batcher.push_text("x" * 10)

    assert len(updates) == 1
    assert updates[0].delta_text == "x" * 10
    assert updates[0].full_text == "hey" + ("x" * 10)


def test_text_stream_batcher_final_flush_and_block_switching() -> None:
    batcher = TextStreamBatcher(flush_interval=999, min_chars=100)

    assert batcher.push_text("answer") == []
    updates = batcher.push_text(
        "thinking",
        block=StreamBlock(id="think-1", type="thinking", title="Thinking"),
    )

    assert len(updates) == 1
    assert updates[0].block_id == "answer"
    assert updates[0].delta_text == "answer"

    final_updates = batcher.finish_all()

    assert len(final_updates) == 1
    assert final_updates[0].block_id == "think-1"
    assert final_updates[0].delta_text == "thinking"
    assert final_updates[0].is_final is True


def test_text_stream_batcher_marks_large_blocks_foldable() -> None:
    batcher = TextStreamBatcher(flush_interval=0, fold_threshold=5)

    updates = batcher.push_text("abcdef")

    assert len(updates) == 1
    assert updates[0].foldable is True


def test_render_stream_event_delta_maps_agent_details() -> None:
    answer = render_stream_event_delta(
        PartStartEvent(index=0, part=TextPart(content="hello"))
    )
    answer_delta = render_stream_event_delta(
        PartDeltaEvent(index=0, delta=TextPartDelta(content_delta=" world"))
    )
    thinking = render_stream_event_delta(
        PartStartEvent(index=1, part=ThinkingPart(content="checking"))
    )
    thinking_delta = render_stream_event_delta(
        PartDeltaEvent(index=1, delta=ThinkingPartDelta(content_delta=" docs"))
    )
    tool_call = render_stream_event_delta(
        FunctionToolCallEvent(
            ToolCallPart(
                tool_name="lookup",
                args={"token": "secret", "query": "octomate"},
                tool_call_id="call_1",
            )
        )
    )
    tool_result = render_stream_event_delta(
        FunctionToolResultEvent(
            ToolReturnPart(
                tool_name="lookup",
                content={"ok": True},
                tool_call_id="call_1",
            )
        )
    )

    assert answer is not None
    assert answer.block.type == "answer"
    assert answer.text == "hello"
    assert answer_delta is not None
    assert answer_delta.text == " world"
    assert thinking is not None
    assert thinking.block.type == "thinking"
    assert thinking.text == "checking"
    assert thinking_delta is not None
    assert thinking_delta.text == " docs"
    assert tool_call is not None
    assert tool_call.block.type == "tool_call"
    assert "secret" in tool_call.text
    assert tool_result is not None
    assert tool_result.block.type == "tool_result"
    assert '"ok":true' in tool_result.text.replace(" ", "")
