"""Channel-generic feelers and output helpers: markdown rendering, plain-text
deferred feelers, `Feelers.present_actions`, stream batching, and chunking."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator
from typing import cast

import pytest
from pydantic_ai.messages import (
    FunctionToolCallEvent,
    FunctionToolResultEvent,
    PartDeltaEvent,
    PartStartEvent,
    RetryPromptPart,
    TextPart,
    TextPartDelta,
    ThinkingPart,
    ThinkingPartDelta,
    ToolCallPart,
    ToolReturnPart,
)
from pydantic_ai.tools import DeferredToolRequests
from uuid_utils.compat import uuid7

from octomate.managers.deferred import DeferredActionManager
from octomate.schemas.conversation import Conversation, ChannelAddress
from octomate.schemas.deferred import (
    ApprovalRequest,
    DeferredApproval,
    DeferredQuestion,
    QuestionRequest,
)
from octomate.schemas.segments import MarkdownSegment, TextSegment
from octomate.schemas.triage import SummonDecision
from octomate.tentacles.channel.feelers.base import Feelers
from octomate.tentacles.channel.feelers.deferred import (
    PlainTextApprovalFeeler,
    PlainTextAskQuestionFeeler,
)
from octomate.tentacles.channel.feelers.output import (
    MarkdownChunker,
    StreamBlock,
    TextStreamBatcher,
    markdown_from_output,
    render_stream_event_delta,
    should_skip_plan_tool,
)
from octomate.tentacles.channel.lark import LarkTentacle
from octomate.tentacles.channel.napcat import NapcatTentacle
from octomate.tentacles.channel.slack import SlackTentacle
from octomate.tentacles.channel.slack.ink import SLACK_MARKDOWN_TEXT_LIMIT

from tests.support.channels import (
    FakeChannelTentacle,
    NoopSegmentsFeeler,
    NoopTimeline,
    RecordingApprovalFeeler,
    RecordingMarkdownFeeler,
    RecordingQuestionFeeler,
    RecordingTimeline,
    bound,
    drive,
)
from tests.support.managers import FakeActionManager, FakePresentedBatch
from tests.support.scenarios import mid_run_notice, play


def _key(channel: str = "im") -> ChannelAddress:
    return ChannelAddress(
        channel_tentacle_id=channel,
        chat_type="private",
        chat_id="alice",
        user_id="alice",
        thread_id="thread-1",
    )


def _question(
    *,
    batch_id: uuid.UUID | None = None,
    position: int = 0,
    question: str = "Continue?",
    choices: list[str] | None = None,
    hint: str = "",
) -> DeferredQuestion:
    args: QuestionRequest = {"question": question}
    if choices is not None:
        args["choices"] = choices
    if hint:
        args["hint"] = hint
    return DeferredQuestion(
        id=uuid7(),
        batch_id=batch_id or uuid7(),
        tool_name="ask_questions",
        tool_call_id="call_questions",
        position=position,
        args=args,
    )


def _approval(*, batch_id: uuid.UUID | None = None) -> DeferredApproval:
    return DeferredApproval(
        id=uuid7(),
        batch_id=batch_id or uuid7(),
        tool_name="shell",
        tool_call_id="call_approval",
        args=ApprovalRequest(tool_name="shell", args={"cmd": "git status"}),
    )


def test_thread_strategies_are_declared() -> None:
    assert SlackTentacle.thread_strategy == "flat_thread"
    assert LarkTentacle.thread_strategy == "flat_thread"
    assert NapcatTentacle.thread_strategy == "main_only"


def test_parent_timeline_hides_subagent_tool_rows() -> None:
    assert should_skip_plan_tool("scheme")
    assert should_skip_plan_tool("whisper")


async def test_timeline_pairs_parallel_subagent_calls_with_their_results() -> None:
    channel = FakeChannelTentacle()
    timeline = RecordingTimeline()
    bound(timeline, channel, _key())
    events = [
        FunctionToolCallEvent(
            ToolCallPart(
                tool_name="scheme",
                args={"name": "audit", "brief": "Audit the repo."},
                tool_call_id="call-a",
            )
        ),
        FunctionToolCallEvent(
            ToolCallPart(
                tool_name="scheme",
                args={"name": "tests", "brief": "Run tests."},
                tool_call_id="call-b",
            )
        ),
        FunctionToolCallEvent(
            ToolCallPart(
                tool_name="scheme",
                args={"name": "docs", "brief": "Review docs."},
                tool_call_id="call-c",
            )
        ),
        FunctionToolResultEvent(
            ToolReturnPart(
                tool_name="scheme", content="test report", tool_call_id="call-b"
            )
        ),
        FunctionToolResultEvent(
            ToolReturnPart(
                tool_name="scheme", content="audit report", tool_call_id="call-a"
            )
        ),
        FunctionToolResultEvent(
            ToolReturnPart(
                tool_name="scheme", content="docs report", tool_call_id="call-c"
            )
        ),
    ]

    async with timeline.open(_key()) as state:
        await state.drive(play(events))

    assert len(timeline.subagent_states) == 3
    states = {
        state.activity.invocation_id: state for state in timeline.subagent_states
    }
    assert states["call-a"].response == "audit report"
    assert states["call-b"].response == "test report"
    assert states["call-c"].response == "docs report"
    assert len({id(state) for state in timeline.subagent_states}) == 3
    assert all(
        state.settlements == [("completed", None)]
        for state in timeline.subagent_states
    )
    assert all(state.closed for state in timeline.subagent_states)
    assert "tool_start" not in timeline.names()
    assert "tool_end" not in timeline.names()


async def test_each_whisper_tool_call_opens_a_fresh_timeline() -> None:
    channel = FakeChannelTentacle()
    timeline = RecordingTimeline()
    bound(timeline, channel, _key())
    events = [
        FunctionToolCallEvent(
            ToolCallPart(
                tool_name="whisper",
                args={"name": "audit", "message": "Go deeper."},
                tool_call_id="call-1",
            )
        ),
        FunctionToolCallEvent(
            ToolCallPart(
                tool_name="whisper",
                args={"name": "audit", "message": "Summarize."},
                tool_call_id="call-2",
            )
        ),
        FunctionToolResultEvent(
            ToolReturnPart(
                tool_name="whisper",
                content="deep report",
                tool_call_id="call-1",
            )
        ),
        FunctionToolResultEvent(
            ToolReturnPart(
                tool_name="whisper",
                content="summary",
                tool_call_id="call-2",
            )
        ),
    ]
    async with timeline.open(_key()) as state:
        await state.drive(play(events))

    first, second = timeline.subagent_states
    assert first.activity.kind == second.activity.kind == "whisper"
    assert first.activity.name == second.activity.name == "audit"
    assert first.activity.invocation_id == "call-1"
    assert second.activity.invocation_id == "call-2"
    assert first.response == "deep report"
    assert second.response == "summary"


async def test_timeline_folds_retry_and_pending_subagent_failures() -> None:
    channel = FakeChannelTentacle()
    timeline = RecordingTimeline()
    bound(timeline, channel, _key())
    events = [
        FunctionToolCallEvent(
            ToolCallPart(
                tool_name="scheme",
                args={"name": "timeout"},
                tool_call_id="call-timeout",
            )
        ),
        FunctionToolResultEvent(
            RetryPromptPart(
                tool_name="scheme",
                content="The accomplice exceeded its timeout.",
                tool_call_id="call-timeout",
            )
        ),
        FunctionToolCallEvent(
            ToolCallPart(
                tool_name="scheme",
                args={"name": "broken"},
                tool_call_id="call-broken",
            )
        ),
    ]

    async with timeline.open(_key()) as state:
        await state.drive(play(events))

    timed_out, broken = timeline.subagent_states
    assert timed_out.response.startswith("The accomplice exceeded its timeout.")
    assert timed_out.settlements == [("failed", None)]
    assert broken.settlements == [("failed", None)]
    assert timed_out.closed and broken.closed


async def test_subagent_renderer_failures_do_not_interrupt_the_parent_stream() -> None:
    events = [
        FunctionToolCallEvent(
            ToolCallPart(
                tool_name="scheme",
                args={"name": "resilient"},
                tool_call_id="call-a",
            )
        ),
        FunctionToolResultEvent(
            ToolReturnPart(
                tool_name="scheme", content="report", tool_call_id="call-a"
            )
        ),
    ]
    for timeline in (
        RecordingTimeline(fail_subagent_open=True),
        RecordingTimeline(fail_subagent_updates=True),
    ):
        channel = FakeChannelTentacle()
        bound(timeline, channel, _key())
        async with timeline.open(_key()) as state:
            await state.drive(play(events))


async def test_timeline_closes_an_unfinished_subagent_on_cancellation() -> None:
    channel = FakeChannelTentacle()
    timeline = RecordingTimeline()
    bound(timeline, channel, _key())
    waiting = asyncio.Event()

    async def source() -> AsyncIterator[FunctionToolCallEvent]:
        yield FunctionToolCallEvent(
            ToolCallPart(
                tool_name="scheme",
                args={"name": "cancelled"},
                tool_call_id="call-a",
            )
        )
        waiting.set()
        await asyncio.Event().wait()

    async def consume() -> None:
        async with timeline.open(_key()) as state:
            await state.drive(source())

    task = asyncio.create_task(consume())
    await waiting.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    [state] = timeline.subagent_states
    assert state.settlements == [("cancelled", None)]
    assert state.closed


async def test_default_timeline_omits_subagent_activity() -> None:
    channel = FakeChannelTentacle()
    events = [
        FunctionToolCallEvent(
            ToolCallPart(
                tool_name="scheme",
                args={"name": "audit"},
                tool_call_id="call-a",
            )
        ),
        FunctionToolResultEvent(
            ToolReturnPart(
                tool_name="scheme",
                content="audit report",
                tool_call_id="call-a",
            )
        ),
    ]

    await drive(channel, _key(), play(events))

    assert channel.sent == []


def test_markdown_from_output_renders_segments() -> None:
    segments = [
        MarkdownSegment(data={"text": "# hi"}),
        TextSegment(data={"text": "plain"}),
    ]
    assert markdown_from_output(segments) == "# hi\n\nplain"
    # An empty reply renders nothing (not an empty message).
    assert markdown_from_output([]) is None


async def test_plain_text_feelers_present_approval_and_questions() -> None:
    markdown = RecordingMarkdownFeeler()
    address = _key()
    approval = _approval()
    questions = [
        _question(
            question="Pick a deploy window", choices=["now", "later"], hint="UTC"
        ),
        _question(question="Who approves?", position=1),
    ]

    approval_ids = await PlainTextApprovalFeeler(markdown).present(
        address,
        [approval],
    )
    question_ids = await PlainTextAskQuestionFeeler(markdown).present(
        address,
        questions,
    )

    assert approval_ids == {approval.id: "markdown-message"}
    assert question_ids == {
        questions[0].id: "markdown-message",
        questions[1].id: "markdown-message",
    }
    sent = markdown.calls
    assert sent[0] == (
        address,
        (
            f"Octomate needs approval for `shell` ({approval.id}). This channel "
            "can show the request, but does not support interactive approval cards yet."
        ),
    )
    assert "Pick a deploy window" in sent[1][1]
    assert "Hint: UTC" in sent[1][1]
    assert "- now" in sent[1][1]
    assert str(questions[0].id) in sent[1][1]
    assert "Who approves?" in sent[2][1]


async def test_feelers_present_actions_creates_batch_splits_and_marks() -> None:
    approval = _approval()
    first = _question(question="First?")
    second = _question(question="Second?", position=1)
    approvals = RecordingApprovalFeeler()
    ask_questions = RecordingQuestionFeeler()
    presented_batch = FakePresentedBatch(
        questions=[first, second], approvals=[approval]
    )
    manager = FakeActionManager(presented_batch=presented_batch)
    source_address = _key("source")
    target_address = _key("target")
    decision = SummonDecision(
        action="summon",
        agent_id="inkling",
        model="test",
        reason="needs input",
        hint="needs input",
        summon="needs input",
    )
    requests = DeferredToolRequests(
        calls=[
            ToolCallPart(
                tool_name="ask_questions",
                args={"questions": [{"question": "First?"}, {"question": "Second?"}]},
                tool_call_id="call_questions",
            )
        ],
        approvals=[
            ToolCallPart(
                tool_name="shell",
                args={"cmd": "git status"},
                tool_call_id="call_approval",
            )
        ],
    )
    conversation = Conversation(
        thread_id=uuid7(),
        agent_tentacle_id="inkling",
    )

    batch = await Feelers(
        markdown=RecordingMarkdownFeeler(),
        timeline=NoopTimeline(),
        segments=NoopSegmentsFeeler(),
        approvals=approvals,
        ask_questions=ask_questions,
    ).present_actions(
        action_manager=cast(DeferredActionManager, manager),
        conversation=conversation,
        agent_tentacle_id="inkling",
        run_name="react",
        source_address=source_address,
        target_address=target_address,
        target_mode="sub",
        decision=decision,
        requests=requests,
    )

    assert batch is presented_batch
    assert manager.create_calls[0].conversation is conversation
    assert manager.create_calls[0].source_address == source_address
    assert manager.create_calls[0].target_address == target_address
    assert approvals.presented == [(target_address, [approval])]
    assert ask_questions.presented == [(target_address, [first, second])]
    assert manager.presented == [
        (approval.id, f"approval-{approval.id}"),
        (first.id, f"question-{first.id}"),
        (second.id, f"question-{second.id}"),
    ]


async def test_default_timeline_rotates_answer_messages() -> None:
    channel = FakeChannelTentacle()
    address = _key()

    async with channel.feelers.timeline.open(address) as state:
        await state.drive(
            play(mid_run_notice(notice="first notice", answer="final answer"))
        )

    assert [sent[2][0]["text"] for sent in channel.sent] == [
        "first notice",
        "final answer",
    ]


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


def test_markdown_chunker_prefers_paragraph_boundaries() -> None:
    chunker = MarkdownChunker()
    first = ("a" * (SLACK_MARKDOWN_TEXT_LIMIT // 2)) + "\n\n"
    second = "b" * (SLACK_MARKDOWN_TEXT_LIMIT // 2)
    text = first + second

    chunks = chunker.chunk(text)

    assert "".join(chunks) == text
    assert all(len(chunk) <= SLACK_MARKDOWN_TEXT_LIMIT for chunk in chunks)
    assert chunks[0] == first


def test_markdown_chunker_prefers_word_boundaries_before_hard_cutting() -> None:
    chunker = MarkdownChunker()
    text = "word " * ((SLACK_MARKDOWN_TEXT_LIMIT // 5) + 10)

    chunks = chunker.chunk(text)

    assert "".join(chunks) == text
    assert all(len(chunk) <= SLACK_MARKDOWN_TEXT_LIMIT for chunk in chunks)
    assert chunks[0].endswith(" ")
