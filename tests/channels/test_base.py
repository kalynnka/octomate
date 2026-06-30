"""ChannelTentacle base pipeline: ingest, consume/drive_timeline, sub-threads."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from uuid import uuid4

import pytest
from pydantic_ai import AgentRunResult, AgentRunResultEvent
from pydantic_ai.messages import (
    FunctionToolCallEvent,
    PartDeltaEvent,
    PartStartEvent,
    TextPart,
    TextPartDelta,
    ThinkingPart,
    ToolCallPart,
)
from pydantic_ai.result import FinalResult
from sqlalchemy.ext.asyncio import AsyncEngine

from octomate.capabilities.events import (
    ActionBatchEvent,
    StreamEvents,
    TodoCompletedEvent,
)
from octomate.schemas.awakes import UserMessageSignal
from octomate.schemas.conversation import ChannelAddress, ChatType
from octomate.schemas.deferred import (
    ApprovalRequest,
    DeferredApproval,
    DeferredQuestion,
)
from octomate.schemas.segments import (
    AtData,
    AtSegment,
    ImageData,
    ImageSegment,
    TextSegment,
)
from octomate.schemas.todos import Todo
from octomate.tentacles.channel.base import (
    ChannelOutput,
    ChannelTentacle,
    DownloadedImage,
)
from tests.support.channels import (
    FakeChannelTentacle,
    FakeChromo,
    FakeOctomate,
    NativeMessage,
    RawMessage,
    RecordingDeferredActions,
    RecordingTimeline,
    bound,
)
from tests.support.scenarios import (
    action_batch,
    message_sent,
    mid_run_notice,
    plain_deferred_requests,
    plain_segments,
    plan_tool_noise,
    play,
    segments_reply,
    showcase,
    streamed_text,
)


@pytest.fixture
def channel() -> FakeChannelTentacle:
    return FakeChannelTentacle(id="chan1")


def _key(
    channel_id: str = "chan1",
    *,
    chat_type: ChatType = "private",
    chat_id: str = "alice",
    user_id: str = "alice",
    thread_id: str = "",
) -> ChannelAddress:
    return ChannelAddress(
        channel_tentacle_id=channel_id,
        chat_type=chat_type,
        chat_id=chat_id,
        user_id=user_id,
        thread_id=thread_id,
    )


async def test_ingest_dispatches_event_to_octomate(
    channel: FakeChannelTentacle,
    in_memory_engine: AsyncEngine,
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
    address = signal.address
    assert signal.trigger_thread_message_id is not None

    assert address.channel_tentacle_id == "chan1"
    assert address.chat_id == "lobby"
    assert address.chat_type == "group"
    assert address.user_id == "alice"

    event = signal.messages[0]
    assert event.tentacle_id == "chan1"
    assert event.self_id == "bot"
    assert event.sender.user_id == "alice"

    thread = await octomate.thread_manager.ensure(address)
    assert thread.messages[-1].id == signal.trigger_thread_message_id
    assert thread.messages[-1].message_text == "hello"


async def test_ingest_swallows_chromo_errors(
    caplog: pytest.LogCaptureFixture,
) -> None:
    class ExplodingChromo(FakeChromo):
        async def sip(self, raw: RawMessage) -> None:
            raise RuntimeError("decode boom")

    channel = FakeChannelTentacle(id="chan1", chromo=ExplodingChromo())

    with caplog.at_level("ERROR"):
        await channel.ingest({"message_id": "m1"})

    octomate = channel.octomate
    assert isinstance(octomate, FakeOctomate)
    assert octomate.kicks == []
    assert "error in ingest" in caplog.text


async def test_group_mention_filter_records_unmentioned_events_before_ignore(
    in_memory_engine: AsyncEngine,
) -> None:
    channel = FakeChannelTentacle(id="chan1")
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

    thread = await octomate.thread_manager.ensure(
        _key(chat_type="group", chat_id="lobby")
    )
    assert [message.message_text for message in thread.messages] == ["hello"]


async def test_next_mention_prompt_includes_stored_unmentioned_messages(
    in_memory_engine: AsyncEngine,
) -> None:
    channel = FakeChannelTentacle(id="chan1")
    await channel.ingest(
        {
            "message_id": "m42",
            "user_id": "alice",
            "chat_id": "lobby",
            "chat_type": "group",
            "segments": [TextSegment(data={"text": "quiet context"})],
        }
    )
    await channel.ingest(
        {
            "message_id": "m43",
            "user_id": "alice",
            "chat_id": "lobby",
            "chat_type": "group",
            "segments": [
                AtSegment(data=AtData(user_id="bot")),
                TextSegment(data={"text": "wake now"}),
            ],
        }
    )

    octomate = channel.octomate
    assert isinstance(octomate, FakeOctomate)
    assert len(octomate.kicks) == 1
    signal = octomate.kicks[0]
    assert isinstance(signal, UserMessageSignal)
    assert signal.trigger_thread_message_id is not None

    thread = await octomate.thread_manager.ensure(
        _key(chat_type="group", chat_id="lobby")
    )
    pending = await octomate.thread_manager.pending_prompt_messages(
        thread,
        signal.trigger_thread_message_id,
        active_agent_id="inkling",
    )
    assert [message.message_text for message in pending] == [
        "quiet context",
        "wake now",
    ]


async def test_submerge_downloads_images_and_rewrites_file(
    channel: FakeChannelTentacle,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    in_memory_engine: AsyncEngine,
) -> None:
    monkeypatch.setattr(FakeChannelTentacle, "FILES_ROOT", tmp_path)
    channel.recording_ink.downloads["remote-image-address"] = DownloadedImage(
        data=b"png-bytes",
        file_name="pic.png",
        content_type="image/png",
        url="https://files.example/pic.png",
    )

    await channel.ingest(
        {
            "message_id": "m7",
            "user_id": "alice",
            "chat_id": "alice",
            "chat_type": "private",
            "segments": [ImageSegment(data=ImageData(file="remote-image-address"))],
        }
    )

    octomate = channel.octomate
    assert isinstance(octomate, FakeOctomate)
    signal = octomate.kicks[0]
    assert isinstance(signal, UserMessageSignal)
    segment = signal.messages[0].segments[0]
    assert isinstance(segment, ImageSegment)
    saved = Path(segment.data.file)
    assert saved.is_relative_to(tmp_path)
    assert saved.read_bytes() == b"png-bytes"
    assert segment.data.url == "https://files.example/pic.png"


async def test_markdown_feeler_encodes_final_agent_result_and_sends_native_messages(
    channel: FakeChannelTentacle,
) -> None:
    await channel.feelers.markdown.present(_key(), "hi alice")

    assert len(channel.sent) == 1
    chat_id, chat_type, messages, reply_to, _ = channel.sent[0]
    assert chat_id == "alice"
    assert chat_type == "private"
    assert messages[0]["text"] == "hi alice"
    assert reply_to is None


async def test_markdown_feeler_uses_conversation_thread_as_reply_target(
    channel: FakeChannelTentacle,
) -> None:
    address = _key(chat_type="group", chat_id="lobby", thread_id="m1")

    await channel.feelers.markdown.present(address, "after reply")

    assert len(channel.sent) == 1
    assert channel.sent[0][3] == "m1"
    assert channel.sent[0][2][0]["text"] == "after reply"


async def test_consume_renders_answer_and_drains_stream(
    channel: FakeChannelTentacle,
    caplog: pytest.LogCaptureFixture,
) -> None:
    drained = False

    async def events() -> AsyncIterator[
        StreamEvents[ChannelOutput] | AgentRunResultEvent[ChannelOutput]
    ]:
        nonlocal drained
        # Thinking + tool passthrough exercise the dispatch; the Default timeline
        # (no streaming transport) drops them and only accumulates the answer text.
        yield PartStartEvent(index=0, part=ThinkingPart(content="hmm"))
        yield FunctionToolCallEvent(
            part=ToolCallPart(tool_name="search", args={"q": "x"}, tool_call_id="t1")
        )
        yield PartStartEvent(index=1, part=TextPart(content="stream "))
        yield PartDeltaEvent(index=1, delta=TextPartDelta(content_delta="me"))
        yield FinalResult[ChannelOutput](output="stream me")
        # Reached only if consume() drains the whole stream past FinalResult.
        drained = True

    with caplog.at_level("WARNING"):
        message_id = await channel.consume(_key(), events())

    assert message_id is not None
    assert len(channel.sent) == 1
    assert channel.sent[0][2][0]["text"] == "stream me"
    assert "no streaming transport" in caplog.text
    assert drained


async def test_consume_renders_raw_text_answer(
    channel: FakeChannelTentacle,
) -> None:
    async def events() -> AsyncIterator[
        StreamEvents[ChannelOutput] | AgentRunResultEvent[ChannelOutput]
    ]:
        # The react graph streams the reply as raw text parts (no normalizer).
        yield PartStartEvent(index=0, part=TextPart(content="raw "))
        yield PartDeltaEvent(index=0, delta=TextPartDelta(content_delta="answer"))
        yield AgentRunResultEvent(AgentRunResult("raw answer"))

    await channel.consume(_key(), events())

    assert len(channel.sent) == 1
    assert channel.sent[0][2][0]["text"] == "raw answer"


async def test_consume_falls_back_to_final_output_when_no_text_streamed(
    channel: FakeChannelTentacle,
) -> None:
    async def events() -> AsyncIterator[
        StreamEvents[ChannelOutput] | AgentRunResultEvent[ChannelOutput]
    ]:
        # No streamed answer text — only the terminal result carries the reply.
        yield AgentRunResultEvent(AgentRunResult("just the final"))

    await channel.consume(_key(), events())

    assert len(channel.sent) == 1
    assert channel.sent[0][2][0]["text"] == "just the final"


async def test_consume_falls_back_to_stringified_final_output(
    channel: FakeChannelTentacle,
) -> None:
    await channel.consume(_key(), play(plain_deferred_requests()))

    assert len(channel.sent) == 1
    assert "DeferredToolRequests" in channel.sent[0][2][0]["text"]


async def test_consume_falls_back_to_final_segments_when_no_segments_streamed(
    channel: FakeChannelTentacle,
) -> None:
    await channel.consume(_key(), play(plain_segments(image_file=None)))

    assert len(channel.sent) == 1
    text = channel.sent[0][2][0]["text"]
    assert "## Scenario" in text
    assert "[card]" in text


async def test_consume_renders_streamed_segments(
    channel: FakeChannelTentacle,
) -> None:
    await channel.consume(_key(), play(segments_reply(image_file=None)))

    assert len(channel.sent) == 1
    text = channel.sent[0][2][0]["text"]
    assert "## Scenario" in text
    assert "[card]" in text


async def test_consume_appends_todo_checklist_to_final_message(
    channel: FakeChannelTentacle,
) -> None:
    todo = Todo(
        conversation_id=uuid4(),
        ref="T1",
        content="Find the docs",
        status="completed",
    )

    async def events() -> AsyncIterator[
        StreamEvents[ChannelOutput] | AgentRunResultEvent[ChannelOutput]
    ]:
        yield TodoCompletedEvent(todo=todo)
        yield PartStartEvent(index=0, part=TextPart(content="done"))

    await channel.consume(_key(), events())

    # The answer-only timeline has no live transport: the latest todo snapshot
    # rides under the final message as a checklist.
    assert len(channel.sent) == 1
    text = channel.sent[0][2][0]["text"]
    assert text.startswith("done")
    assert "Tasks:" in text
    assert "- [x] Find the docs" in text


async def test_consume_renders_and_marks_action_batch(
    channel: FakeChannelTentacle,
) -> None:
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
    actions = RecordingDeferredActions()
    channel = FakeChannelTentacle(
        id="chan1", octomate=FakeOctomate(deferred_actions=actions)
    )

    async def events() -> AsyncIterator[
        StreamEvents[ChannelOutput] | AgentRunResultEvent[ChannelOutput]
    ]:
        yield ActionBatchEvent(
            batch_id="b1", questions=[question], approvals=[approval]
        )

    await channel.consume(_key(), events())

    # The batch renders through the channel's (plaintext) question/approval feelers
    # as a unit, and each presented action is marked with its message id.
    assert len(channel.sent) == 2
    assert "Pick one?" in channel.sent[0][2][0]["text"]
    assert "do_thing" in channel.sent[1][2][0]["text"]
    marked = {str(action_id): message_id for action_id, message_id in actions.marked}
    assert marked[str(question.id)] == "sent-1"
    assert marked[str(approval.id)] == "sent-2"


async def test_consume_renders_streamed_deferred_requests_once(
    channel: FakeChannelTentacle,
) -> None:
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
    actions = RecordingDeferredActions()
    channel = FakeChannelTentacle(
        id="chan1", octomate=FakeOctomate(deferred_actions=actions)
    )

    await channel.consume(
        _key(),
        play(action_batch(batch_id="b1", questions=[question], approvals=[approval])),
    )

    assert len(channel.sent) == 2
    assert "DeferredToolRequests" not in "\n".join(
        message[2][0]["text"] for message in channel.sent
    )


async def test_consume_skips_plan_tool_events(
    channel: FakeChannelTentacle,
) -> None:
    state = RecordingTimeline()

    await bound(state, channel, _key()).drive(play(plan_tool_noise()))

    # The plan tool call AND its paired result are both dropped.
    assert "tool_start" not in state.names()
    assert "tool_end" not in state.names()
    assert "answer_delta" in state.names()


async def test_drive_timeline_renders_message_sent_and_skips_the_tool(
    channel: FakeChannelTentacle,
) -> None:
    state = RecordingTimeline()

    await bound(state, channel, _key()).drive(play(message_sent()))

    # The send_message tool call/result render nothing (skipped); the
    # MessageSentEvent renders its segments as reply content.
    names = state.names()
    assert "tool_start" not in names
    assert "tool_end" not in names
    sent = [segment for name, segment in state.calls if name == "answer_segment"]
    assert any(str(segment) == "progress update" for segment in sent)


async def test_drive_timeline_dispatches_each_event_kind(
    channel: FakeChannelTentacle,
) -> None:
    state = RecordingTimeline()

    await bound(state, channel, _key()).drive(play(showcase()))

    assert state.names() == [
        "thinking_start",
        "thinking_delta",  # PartStart carries initial content
        "thinking_delta",
        "thinking_end",
        "tool_start",
        "tool_end",
        "todo",  # created x2
        "todo",
        "todo",  # status changed
        "todo",  # completed
        "answer_segment",  # markdown + card segments
        "answer_segment",
    ]


async def test_drive_timeline_rotates_on_mid_run_notice(
    channel: FakeChannelTentacle,
) -> None:
    state = RecordingTimeline()

    await bound(state, channel, _key()).drive(play(mid_run_notice()))

    # Rotation fires exactly once: after the notice deltas, before the new
    # round's thinking — never for the in-flight tool's result.
    names = state.names()
    assert names.count("rotate") == 1
    assert names.index("rotate") == names.index("answer_delta") + 2
    assert names[names.index("rotate") + 1] == "thinking_start"


async def test_default_timeline_sends_mid_run_notice_as_own_message(
    channel: FakeChannelTentacle,
) -> None:
    await channel.consume(_key(), play(mid_run_notice()))

    # No streaming transport: the notice flushes as its own message when the
    # run continues, and the final answer arrives separately.
    assert len(channel.sent) == 2
    assert "trying another way" in channel.sent[0][2][0]["text"]
    assert "pinning the dependency" in channel.sent[1][2][0]["text"]


async def test_drive_timeline_keeps_draining_after_render_failure(
    channel: FakeChannelTentacle,
    caplog: pytest.LogCaptureFixture,
) -> None:
    class ExplodingTimeline(RecordingTimeline):
        async def answer_delta(self, text: str) -> None:
            raise RuntimeError("render boom")

    state = ExplodingTimeline()
    drained = False

    async def events() -> AsyncIterator[
        StreamEvents[ChannelOutput] | AgentRunResultEvent[ChannelOutput]
    ]:
        nonlocal drained
        for event in streamed_text("a", "b"):
            yield event
        drained = True

    with caplog.at_level("WARNING"):
        await bound(state, channel, _key()).drive(events())

    assert drained
    assert "timeline render failed" in caplog.text
    # Everything after the failing event is skipped, including the fallback.
    assert state.names() == ["answer_start"]


async def test_start_sub_thread_falls_back_to_main_target(
    caplog: pytest.LogCaptureFixture,
) -> None:
    class MainTargetChannel(FakeChannelTentacle):
        async def start_sub_thread(
            self,
            address: ChannelAddress,
            hint_text: str,
        ) -> ChannelAddress:
            return await ChannelTentacle[RawMessage, NativeMessage].start_sub_thread(
                self, address, hint_text
            )

    channel = MainTargetChannel(id="chan1")
    address = _key()

    with caplog.at_level("WARNING"):
        returned = await channel.start_sub_thread(address, "summon")

    assert returned == address
    assert "does not support sub-thread startup" in caplog.text
    assert len(channel.sent) == 1
    assert channel.sent[0][2][0]["text"] == "summon"


async def test_channel_context_manager_probes_on_enter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    channel = FakeChannelTentacle(id="chan1")
    probed: list[bool] = []
    original = channel.probe

    async def probe() -> None:
        probed.append(True)
        await original()

    monkeypatch.setattr(channel, "probe", probe)

    async with channel as entered:
        assert entered is channel

    assert probed == [True]
