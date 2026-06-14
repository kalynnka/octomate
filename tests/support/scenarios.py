"""Canonical scripted agent-run event scenarios.

Each factory returns the event list a real agent run would stream — the
`StreamEvents` dialect `ChannelTentacle.consume` accepts (see
octomate/capabilities/events.py). The same scripts back the mocked channel
tests, the `@trigger` live-replay tests, and `FakeAgent`'s reception output,
so the whole suite speaks one event dialect.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncGenerator, Sequence

import anyio

from pydantic_ai import AgentRunResult, AgentRunResultEvent
from pydantic_ai.messages import (
    FunctionToolCallEvent,
    FunctionToolResultEvent,
    PartDeltaEvent,
    PartEndEvent,
    PartStartEvent,
    ThinkingPart,
    ThinkingPartDelta,
    TextPart,
    TextPartDelta,
    ToolCallPart,
    ToolCallPartDelta,
    ToolReturnPart,
)
from pydantic_ai.result import FinalResult
from pydantic_ai.tools import DeferredToolRequests

from octomate.capabilities.events import (
    ActionBatchEvent,
    MessageSentEvent,
    ResultSegmentEvent,
    TodoCompletedEvent,
    TodoCreatedEvent,
    TodoStatusChangedEvent,
)
from octomate.capabilities.react import ReactStreamEvent
from octomate.schemas.deferred import (
    ApprovalRequest,
    DeferredApproval,
    DeferredQuestion,
)
from octomate.schemas.segments import (
    CardData,
    CardSegment,
    ImageData,
    ImageSegment,
    MarkdownSegment,
    OutputSegment,
)
from octomate.schemas.todos import Todo
from octomate.tentacles.channel.base import ChannelOutput
from octomate.types.json import JsonObject

ChannelScript = list[ReactStreamEvent[ChannelOutput]]

SCENARIO_CONVERSATION_ID = uuid.UUID(int=0x0C70)


async def play(
    events: Sequence[ReactStreamEvent[ChannelOutput]],
    *,
    delay: float = 0.0,
) -> AsyncGenerator[ReactStreamEvent[ChannelOutput]]:
    """Play a script as the consume stream; `delay` paces the events so a live
    replay actually streams instead of arriving as one burst."""
    for event in events:
        if delay:
            await anyio.sleep(delay)
        yield event


def plain_answer(text: str = "hello from octomate") -> ChannelScript:
    """Only the terminal result — exercises the never-streamed-text fallback."""
    return [AgentRunResultEvent(AgentRunResult(text))]


def streamed_text(*deltas: str) -> ChannelScript:
    deltas = deltas or ("hello ", "from octomate")
    full = "".join(deltas)
    return [
        *text_part_events(*deltas),
        FinalResult[ChannelOutput](output=full),
        AgentRunResultEvent(AgentRunResult(full)),
    ]


def text_part_events(*deltas: str, index: int = 0) -> ChannelScript:
    if not deltas:
        return []
    return [
        PartStartEvent(index=index, part=TextPart(content=deltas[0])),
        *(
            PartDeltaEvent(index=index, delta=TextPartDelta(content_delta=delta))
            for delta in deltas[1:]
        ),
    ]


def scenario_card_payload() -> JsonObject:
    return {
        "schema": "2.0",
        "header": {
            "title": {"tag": "plain_text", "content": "Scenario card"},
        },
        "body": {
            "elements": [{"tag": "markdown", "content": "card payload"}],
        },
    }


def slack_card_payload() -> JsonObject:
    return {
        "blocks": [
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": "*Scenario card*\ncard payload",
                },
            }
        ],
    }


def reply_segments(
    *,
    image_file: str | None = "/tmp/octomate-scenario.png",
    card_payload: JsonObject | None = None,
) -> list[OutputSegment]:
    if card_payload is None:
        card_payload = scenario_card_payload()
    segments: list[OutputSegment] = [
        MarkdownSegment(
            data={"text": "## Scenario\nA *markdown* reply segment."},
        ),
        CardSegment(data=CardData(payload=card_payload)),
    ]
    if image_file is not None:
        segments.insert(
            1,
            ImageSegment(
                data=ImageData(
                    file=image_file,
                    name="scenario.png",
                    summary="a scenario image",
                ),
            ),
        )
    return segments


def segments_reply(
    *,
    image_file: str | None = "/tmp/octomate-scenario.png",
    card_payload: JsonObject | None = None,
) -> ChannelScript:
    segments = reply_segments(image_file=image_file, card_payload=card_payload)
    return [
        *segment_result_events(segments),
        FinalResult[ChannelOutput](output=segments),
        AgentRunResultEvent(AgentRunResult(segments)),
    ]


def segment_result_events(
    segments: list[OutputSegment], *, index: int = 0
) -> ChannelScript:
    payload = json.dumps(
        {"response": [segment.model_dump(mode="json") for segment in segments]},
        ensure_ascii=False,
        separators=(",", ":"),
    )
    split_at = max(1, len(payload) // 2)
    return [
        PartStartEvent(
            index=index,
            part=ToolCallPart(
                tool_name="final_result",
                args=payload[:split_at],
                tool_call_id=f"call_final_result_{index}",
            ),
        ),
        PartDeltaEvent(
            index=index,
            delta=ToolCallPartDelta(args_delta=payload[split_at:]),
        ),
        *(ResultSegmentEvent(segment=segment) for segment in segments),
    ]


def plain_segments(
    *,
    image_file: str | None = "/tmp/octomate-scenario.png",
    card_payload: JsonObject | None = None,
) -> ChannelScript:
    """Only the terminal result, carrying a list of message segments."""
    return [
        AgentRunResultEvent(
            AgentRunResult(
                reply_segments(image_file=image_file, card_payload=card_payload)
            )
        )
    ]


def thinking_and_tools(answer: str = "the lookup finished cleanly") -> ChannelScript:
    return [
        PartStartEvent(index=0, part=ThinkingPart(content="Let me check")),
        PartDeltaEvent(index=0, delta=ThinkingPartDelta(content_delta=" the docs.")),
        PartEndEvent(index=0, part=ThinkingPart(content="Let me check the docs.")),
        FunctionToolCallEvent(
            part=ToolCallPart(
                tool_name="lookup",
                args={"query": "octomate"},
                tool_call_id="call_lookup_1",
            )
        ),
        FunctionToolResultEvent(
            ToolReturnPart(
                tool_name="lookup",
                content={"ok": True},
                tool_call_id="call_lookup_1",
            )
        ),
        *streamed_text(answer),
    ]


def scenario_todos() -> tuple[Todo, Todo]:
    """The (completed, in-progress) pair the todo scenarios mutate."""
    plan = Todo(
        conversation_id=SCENARIO_CONVERSATION_ID,
        ref="T1",
        content="Outline the plan",
        status="completed",
    )
    docs = Todo(
        conversation_id=SCENARIO_CONVERSATION_ID,
        ref="T2",
        content="Find the docs",
        active_form="Finding the docs",
        status="in_progress",
        position=1,
    )
    return plan, docs


def todo_progress(answer: str = "tasks updated") -> ChannelScript:
    plan, docs = scenario_todos()
    return [
        TodoCreatedEvent(todo=plan.model_copy(update={"status": "pending"})),
        TodoCreatedEvent(todo=docs.model_copy(update={"status": "pending"})),
        TodoStatusChangedEvent(
            todo=docs,
            previous=docs.model_copy(update={"status": "pending"}),
        ),
        TodoCompletedEvent(todo=plan),
        *streamed_text(answer),
    ]


def batch_actions() -> tuple[DeferredQuestion, DeferredApproval]:
    question = DeferredQuestion(
        tool_name="ask_questions",
        tool_call_id="call_ask_1",
        args={"question": "Which option should I take?", "choices": ["A", "B"]},
    )
    approval = DeferredApproval(
        tool_name="deploy",
        tool_call_id="call_deploy_1",
        args=ApprovalRequest(tool_name="deploy"),
    )
    return question, approval


def batch_requests() -> DeferredToolRequests:
    return DeferredToolRequests(
        calls=[
            ToolCallPart(
                tool_name="ask_questions",
                args={"questions": [{"question": "Which option should I take?"}]},
                tool_call_id="call_ask_1",
            )
        ],
        approvals=[
            ToolCallPart(tool_name="deploy", args={}, tool_call_id="call_deploy_1")
        ],
    )


def plain_deferred_requests() -> ChannelScript:
    """Only the terminal result, carrying deferred tool requests."""
    return [AgentRunResultEvent(AgentRunResult(batch_requests()))]


def action_batch(
    *,
    batch_id: str = "batch-1",
    questions: list[DeferredQuestion] | None = None,
    approvals: list[DeferredApproval] | None = None,
) -> ChannelScript:
    """A suspended run: the batch event, then the deferred terminal result."""
    if questions is None or approvals is None:
        question, approval = batch_actions()
        questions = [question] if questions is None else questions
        approvals = [approval] if approvals is None else approvals
    return [
        ActionBatchEvent(batch_id=batch_id, questions=questions, approvals=approvals),
        AgentRunResultEvent(AgentRunResult(batch_requests())),
    ]


def plan_tool_noise(answer: str = "no plan rendered") -> ChannelScript:
    """A tool call/result pair `should_skip_plan_tool` must drop."""
    return [
        FunctionToolCallEvent(
            part=ToolCallPart(
                tool_name="ask_questions",
                args={"questions": []},
                tool_call_id="call_plan_1",
            )
        ),
        FunctionToolResultEvent(
            ToolReturnPart(
                tool_name="ask_questions",
                content="deferred",
                tool_call_id="call_plan_1",
            )
        ),
        *streamed_text(answer),
    ]


def message_sent(
    segments: list[OutputSegment] | None = None,
    *,
    answer: str = "all set",
) -> ChannelScript:
    """A `send_message` tool call/result pair (which the timeline skips) plus the
    `MessageSentEvent` the capability injects, then the closing streamed reply."""
    if segments is None:
        segments = [MarkdownSegment(data={"text": "progress update"})]
    event = MessageSentEvent(segments=segments)
    return [
        FunctionToolCallEvent(
            part=ToolCallPart(
                tool_name="send_message",
                args={"segments": [seg.model_dump() for seg in segments]},
                tool_call_id="call_send_1",
            )
        ),
        FunctionToolResultEvent(
            ToolReturnPart(
                tool_name="send_message",
                content="sent",
                tool_call_id="call_send_1",
                metadata=[event],
            )
        ),
        event,
        *streamed_text(answer),
    ]


def agent_run() -> ChannelScript:
    """A full mocked agent run as the react graph streams it: two rounds of
    thinking and tool work with todo progress in between, then the reply as
    streamed text deltas. The timeline scenario — exercises every lifecycle
    hook a multi-step run drives."""
    plan, docs = scenario_todos()
    answer_deltas = (
        "Looked through the docs",
        " and the build logs — ",
        "the flake comes from an unpinned dependency. ",
        "Pinning it in the lockfile fixes the failure.",
    )
    answer = "".join(answer_deltas)
    return [
        PartStartEvent(index=0, part=ThinkingPart(content="The user asks about")),
        PartDeltaEvent(index=0, delta=ThinkingPartDelta(content_delta=" a flaky build.")),
        PartDeltaEvent(
            index=0,
            delta=ThinkingPartDelta(content_delta=" I should plan and check the docs."),
        ),
        PartEndEvent(
            index=0,
            part=ThinkingPart(
                content="The user asks about a flaky build. "
                "I should plan and check the docs."
            ),
        ),
        TodoCreatedEvent(todo=plan.model_copy(update={"status": "pending"})),
        TodoCreatedEvent(todo=docs.model_copy(update={"status": "pending"})),
        FunctionToolCallEvent(
            part=ToolCallPart(
                tool_name="lookup",
                args={"query": "flaky build"},
                tool_call_id="call_lookup_1",
            )
        ),
        FunctionToolResultEvent(
            ToolReturnPart(
                tool_name="lookup",
                content={"matches": 3, "best": "ci-troubleshooting.md"},
                tool_call_id="call_lookup_1",
            )
        ),
        TodoCompletedEvent(todo=plan),
        TodoStatusChangedEvent(
            todo=docs,
            previous=docs.model_copy(update={"status": "pending"}),
        ),
        PartStartEvent(
            index=1,
            part=ThinkingPart(content="The docs point at the dependency lockfile."),
        ),
        PartEndEvent(
            index=1,
            part=ThinkingPart(content="The docs point at the dependency lockfile."),
        ),
        FunctionToolCallEvent(
            part=ToolCallPart(
                tool_name="read_logs",
                args={"job": "build", "tail": 50},
                tool_call_id="call_logs_1",
            )
        ),
        FunctionToolResultEvent(
            ToolReturnPart(
                tool_name="read_logs",
                content={"failed_step": "install", "hint": "unpinned dependency"},
                tool_call_id="call_logs_1",
            )
        ),
        TodoCompletedEvent(todo=docs.model_copy(update={"status": "completed"})),
        *text_part_events(*answer_deltas),
        FinalResult[ChannelOutput](output=answer),
        AgentRunResultEvent(AgentRunResult(answer)),
    ]


def mid_run_notice(
    notice: str = "The docs don't cover this — I'm trying another way.",
    answer: str = "Found it: pinning the dependency fixes the flake.",
) -> ChannelScript:
    """A run that pauses to tell the user it is changing course: one round of
    timeline work, a streamed notice, then a second round — with the first
    round's slow tool finishing only after the new round began — and the final
    answer. Exercises the consumers' timeline rotation."""
    return [
        PartStartEvent(index=0, part=ThinkingPart(content="Checking the docs first.")),
        PartEndEvent(index=0, part=ThinkingPart(content="Checking the docs first.")),
        FunctionToolCallEvent(
            part=ToolCallPart(
                tool_name="lookup",
                args={"query": "flaky build"},
                tool_call_id="call_slow_1",
            )
        ),
        # The notice streams while lookup is still running.
        *text_part_events(
            notice[: len(notice) // 2],
            notice[len(notice) // 2 :],
            index=1,
        ),
        # A new round begins: consumers rotate to a fresh timeline; the
        # in-flight lookup still finishes into the previous one.
        PartStartEvent(
            index=1, part=ThinkingPart(content="Reading the build logs instead.")
        ),
        PartEndEvent(
            index=1, part=ThinkingPart(content="Reading the build logs instead.")
        ),
        FunctionToolResultEvent(
            ToolReturnPart(
                tool_name="lookup",
                content={"matches": 0},
                tool_call_id="call_slow_1",
            )
        ),
        FunctionToolCallEvent(
            part=ToolCallPart(
                tool_name="read_logs",
                args={"job": "build", "tail": 50},
                tool_call_id="call_logs_1",
            )
        ),
        FunctionToolResultEvent(
            ToolReturnPart(
                tool_name="read_logs",
                content={"hint": "unpinned dependency"},
                tool_call_id="call_logs_1",
            )
        ),
        *streamed_text(answer),
    ]


def showcase(
    *,
    image_file: str | None = None,
    card_payload: JsonObject | None = None,
) -> ChannelScript:
    """Thinking + tools + todos + segment reply — the visual-inspection script."""
    plan, docs = scenario_todos()
    segments = reply_segments(image_file=image_file, card_payload=card_payload)
    return [
        PartStartEvent(index=0, part=ThinkingPart(content="Planning the showcase")),
        PartDeltaEvent(index=0, delta=ThinkingPartDelta(content_delta=" run.")),
        PartEndEvent(index=0, part=ThinkingPart(content="Planning the showcase run.")),
        FunctionToolCallEvent(
            part=ToolCallPart(
                tool_name="lookup",
                args={"query": "octomate"},
                tool_call_id="call_lookup_1",
            )
        ),
        FunctionToolResultEvent(
            ToolReturnPart(
                tool_name="lookup",
                content={"ok": True},
                tool_call_id="call_lookup_1",
            )
        ),
        TodoCreatedEvent(todo=plan.model_copy(update={"status": "pending"})),
        TodoCreatedEvent(todo=docs.model_copy(update={"status": "pending"})),
        TodoStatusChangedEvent(
            todo=docs,
            previous=docs.model_copy(update={"status": "pending"}),
        ),
        TodoCompletedEvent(todo=plan),
        *segment_result_events(segments, index=1),
        FinalResult[ChannelOutput](output=segments),
        AgentRunResultEvent(AgentRunResult(segments)),
    ]
