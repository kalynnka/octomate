"""Channel-generic feelers and output helpers: markdown rendering, plain-text
deferred feelers, `Feelers.present_actions`, stream batching, and chunking."""

from __future__ import annotations

import uuid
from typing import cast

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
from uuid_utils.compat import uuid7

from octomate.capabilities.harness.events import (
    ActionBatchEvent,
    GatewayEvent,
    RunErrorEvent,
    SubagentSettledEvent,
    SubagentStartedEvent,
)
from octomate.managers.deferred import DeferredActionManager
from octomate.schemas.conversation import ChannelAddress
from octomate.schemas.deferred import (
    ApprovalRequest,
    DeferredApproval,
    DeferredQuestion,
    QuestionRequest,
)
from octomate.schemas.segments import MarkdownSegment, TextSegment
from octomate.tentacles.channel import ChannelSurfaces, Ink
from octomate.tentacles.feelers.base import Feelers
from octomate.tentacles.feelers.deferred import (
    PlainTextApprovalFeeler,
    PlainTextAskQuestionFeeler,
)
from octomate.tentacles.feelers.oauth import PlainTextOAuthFeeler
from octomate.tentacles.feelers.output import (
    MarkdownChunker,
    StreamBlock,
    TextStreamBatcher,
    markdown_from_output,
    render_stream_event_delta,
    should_skip_plan_tool,
)
from octomate.tentacles.lark import LarkTentacle
from octomate.tentacles.napcat import NapcatTentacle
from octomate.tentacles.slack import SlackTentacle
from octomate.tentacles.slack.ink import SLACK_MARKDOWN_TEXT_LIMIT
from tests.support.channels import (
    FakeChannelTentacle,
    FakeOAuthInk,
    FakeOctomate,
    NoopSegmentsFeeler,
    NoopTimeline,
    RecordingApprovalFeeler,
    RecordingMarkdownFeeler,
    RecordingQuestionFeeler,
    RecordingTimeline,
    bound,
    drive,
)
from tests.support.managers import FakeActionManager
from tests.support.scenarios import mid_run_notice, play


def _key(channel: str = "im") -> ChannelAddress:
    return ChannelAddress(
        channel_tentacle_id=channel,
        chat_type="thread",
        chat_id="alice",
        user_id="alice",
        channel_thread_id="thread-1",
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


def test_channel_surfaces_and_routing_are_declared_apart() -> None:
    # `thread_strategy` is routing: does an inbound threaded message continue its
    # thread without triage. `surfaces` is capability: what this platform can be asked
    # to open.
    assert SlackTentacle.thread_strategy == "flat_thread"
    assert SlackTentacle.surfaces == ChannelSurfaces(
        sub_thread=True, direct_message=True
    )

    assert LarkTentacle.thread_strategy == "flat_thread"
    assert LarkTentacle.surfaces == ChannelSurfaces(
        sub_thread=True, direct_message=True
    )

    # DM-capable but thread-incapable: the pair that proves they are independent.
    assert NapcatTentacle.thread_strategy == "main_only"
    assert NapcatTentacle.surfaces == ChannelSurfaces(
        sub_thread=False, direct_message=True
    )


def test_parent_timeline_hides_subagent_tool_rows() -> None:
    assert should_skip_plan_tool("commission")
    assert should_skip_plan_tool("whisper")
    # `scheme` is a routing spell like `summon`: the timeline draws its row.
    assert not should_skip_plan_tool("scheme")
    assert not should_skip_plan_tool("summon")


async def test_timeline_pairs_parallel_subagents_with_their_results() -> None:
    channel = FakeChannelTentacle()
    timeline = RecordingTimeline()
    bound(timeline, channel, _key())
    events = [
        SubagentStartedEvent(invocation_id="call-a", kind="commission", name="audit"),
        SubagentStartedEvent(invocation_id="call-b", kind="commission", name="tests"),
        SubagentStartedEvent(invocation_id="call-c", kind="commission", name="docs"),
        SubagentSettledEvent(
            invocation_id="call-b", status="completed", response="test report"
        ),
        SubagentSettledEvent(
            invocation_id="call-a", status="completed", response="audit report"
        ),
        SubagentSettledEvent(
            invocation_id="call-c", status="completed", response="docs report"
        ),
    ]

    async with timeline.open(_key()) as state:
        await state.drive(play(events))

    assert len(timeline.subagent_states) == 3
    states = {state.activity.invocation_id: state for state in timeline.subagent_states}
    assert states["call-a"].response == "audit report"
    assert states["call-b"].response == "test report"
    assert states["call-c"].response == "docs report"
    assert all(
        state.settlements == [("completed", None)] for state in timeline.subagent_states
    )
    assert all(state.closed for state in timeline.subagent_states)


async def test_each_whisper_opens_a_fresh_timeline() -> None:
    channel = FakeChannelTentacle()
    timeline = RecordingTimeline()
    bound(timeline, channel, _key())
    events = [
        SubagentStartedEvent(invocation_id="call-1", kind="whisper", name="audit"),
        SubagentStartedEvent(invocation_id="call-2", kind="whisper", name="audit"),
        SubagentSettledEvent(
            invocation_id="call-1", status="completed", response="deep report"
        ),
        SubagentSettledEvent(
            invocation_id="call-2", status="completed", response="summary"
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


async def test_subagent_renderer_failures_do_not_interrupt_the_parent_stream() -> None:
    events = [
        SubagentStartedEvent(invocation_id="call-a", kind="commission", name="ok"),
        SubagentSettledEvent(
            invocation_id="call-a", status="completed", response="report"
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


async def test_default_timeline_omits_subagent_activity() -> None:
    channel = FakeChannelTentacle()
    events = [
        SubagentStartedEvent(invocation_id="call-a", kind="commission", name="audit"),
        SubagentSettledEvent(
            invocation_id="call-a", status="completed", response="audit report"
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


async def test_feelers_present_actions_splits_and_marks() -> None:
    approval = _approval()
    first = _question(question="First?")
    second = _question(question="Second?", position=1)
    approvals = RecordingApprovalFeeler()
    ask_questions = RecordingQuestionFeeler()
    manager = FakeActionManager()
    target_address = _key("target")

    await Feelers(
        markdown=RecordingMarkdownFeeler(),
        timeline=NoopTimeline(),
        segments=NoopSegmentsFeeler(),
        approvals=approvals,
        ask_questions=ask_questions,
        oauth=PlainTextOAuthFeeler(
            cast(Ink[str], FakeOAuthInk()), RecordingMarkdownFeeler()
        ),
    ).present_actions(
        target_address,
        ActionBatchEvent(
            batch_id=str(uuid7()), questions=[first, second], approvals=[approval]
        ),
        action_manager=cast(DeferredActionManager, manager),
    )

    assert approvals.presented == [(target_address, [approval])]
    assert ask_questions.presented == [(target_address, [first, second])]
    assert manager.presented == [
        (approval.id, f"approval-{approval.id}"),
        (first.id, f"question-{first.id}"),
        (second.id, f"question-{second.id}"),
    ]


class SettlingTimeline(NoopTimeline):
    settled: int = 0

    async def actions_presented(self) -> None:
        self.settled += 1


class HiccupTimeline(NoopTimeline):
    async def actions_presented(self) -> None:
        raise RuntimeError("render hiccup")


def plain_feelers() -> Feelers:
    return Feelers(
        markdown=RecordingMarkdownFeeler(),
        timeline=NoopTimeline(),
        segments=NoopSegmentsFeeler(),
        approvals=RecordingApprovalFeeler(),
        ask_questions=RecordingQuestionFeeler(),
        oauth=PlainTextOAuthFeeler(
            cast(Ink[str], FakeOAuthInk()), RecordingMarkdownFeeler()
        ),
    )


async def present_one_approval(timeline: NoopTimeline) -> list[tuple[str, object]]:
    """Drive one approval batch through `timeline`, as a live run's stream does
    when it pauses on a human; answers what the channel's approval feeler drew."""
    channel = FakeChannelTentacle(
        octomate=FakeOctomate(
            deferred_actions=cast(DeferredActionManager, FakeActionManager())
        )
    )
    approvals = RecordingApprovalFeeler()
    channel.feelers.approvals = approvals
    bound(timeline, channel, _key("target"))
    async with timeline.open(_key("target")) as state:
        await state.drive(
            play([ActionBatchEvent(batch_id=str(uuid7()), approvals=[_approval()])])
        )
    return [(str(address), actions) for address, actions in approvals.presented]


async def test_a_presented_batch_settles_the_timeline() -> None:
    """The run is parked on a human once its batch is drawn, so a surface with live
    status stops claiming the agent is thinking."""
    timeline = SettlingTimeline()

    drawn = await present_one_approval(timeline)

    assert len(drawn) == 1
    assert timeline.settled == 1


async def test_a_settle_hiccup_does_not_fail_the_presentation() -> None:
    # The cards are the load-bearing part; the surface settle is a UI hint,
    # and its failure must not cancel the approval riding on the batch.
    drawn = await present_one_approval(HiccupTimeline())

    assert len(drawn) == 1


async def test_feelers_present_a_move_by_its_line_and_a_failure_by_its_trace() -> None:
    feelers = plain_feelers()
    address = _key()

    moved = await feelers.present(
        address,
        GatewayEvent(
            action="teleport",
            destination=_key("far"),
            announcement="Continuing over there.",
        ),
    )
    # A summon leaves no line where it happened.
    silent = await feelers.present(
        address, GatewayEvent(action="summon", destination=address)
    )
    await feelers.present(address, RunErrorEvent(message="boom", trace_id="abc123"))

    assert moved == "markdown-message"
    assert silent is None
    assert cast(RecordingMarkdownFeeler, feelers.markdown).calls == [
        (address, "Continuing over there."),
        (
            address,
            "Something went wrong while handling your message. "
            "Reference id for tracing the issue: `abc123`.",
        ),
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

    first = batcher.push_text("he")
    assert first[0].delta_text == "he"
    assert batcher.push_text("l") == []
    now = 0.3
    updates = batcher.push_text("lo")

    assert len(updates) == 1
    assert updates[0].delta_text == "llo"
    assert updates[0].full_text == "hello"

    updates = batcher.push_text("x" * 10)

    assert len(updates) == 1
    assert updates[0].delta_text == "x" * 10
    assert updates[0].full_text == "hello" + ("x" * 10)


def test_text_stream_batcher_final_flush_and_block_switching() -> None:
    batcher = TextStreamBatcher(flush_interval=999, min_chars=100)

    assert batcher.push_text("answer")[0].delta_text == "answer"
    assert batcher.push_text(" rest") == []
    thinking = StreamBlock(id="think-1", type="thinking", title="Thinking")
    updates = batcher.push_text("thinking", block=thinking)

    assert len(updates) == 2
    assert updates[0].block_id == "answer"
    assert updates[0].delta_text == " rest"
    assert updates[1].delta_text == "thinking"
    assert batcher.push_text(" more", block=thinking) == []

    final_updates = batcher.finish_all()

    assert len(final_updates) == 1
    assert final_updates[0].block_id == "think-1"
    assert final_updates[0].delta_text == " more"
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
