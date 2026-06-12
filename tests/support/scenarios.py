"""Canonical scripted agent-run event scenarios.

Each factory returns the event list a real agent run would stream — the
`StreamEvents` dialect `ChannelTentacle.consume` accepts (see
octomate/capabilities/events.py). The same scripts back the mocked channel
tests, the `@trigger` live-replay tests, and `FakeAgent`'s reception output,
so the whole suite speaks one event dialect.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncGenerator, Sequence

from pydantic_ai import AgentRunResult, AgentRunResultEvent
from pydantic_ai.messages import (
    FunctionToolCallEvent,
    FunctionToolResultEvent,
    PartDeltaEvent,
    PartEndEvent,
    PartStartEvent,
    ThinkingPart,
    ThinkingPartDelta,
    ToolCallPart,
    ToolReturnPart,
)
from pydantic_ai.result import FinalResult
from pydantic_ai.tools import DeferredToolRequests

from octomate.capabilities.events import (
    ActionBatchEvent,
    ResultSegmentEvent,
    ResultTextDeltaEvent,
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
    MessageSegment,
)
from octomate.schemas.todos import Todo
from octomate.tentacles.channel.base import ChannelOutput

ChannelScript = list[ReactStreamEvent[ChannelOutput]]

SCENARIO_CONVERSATION_ID = uuid.UUID(int=0x0C70)


async def play(
    events: Sequence[ReactStreamEvent[ChannelOutput]],
) -> AsyncGenerator[ReactStreamEvent[ChannelOutput]]:
    for event in events:
        yield event


def plain_answer(text: str = "hello from octomate") -> ChannelScript:
    """Only the terminal result — exercises the never-streamed-text fallback."""
    return [AgentRunResultEvent(AgentRunResult(text))]


def streamed_text(*deltas: str) -> ChannelScript:
    deltas = deltas or ("hello ", "from octomate")
    full = "".join(deltas)
    return [
        *(ResultTextDeltaEvent(delta=delta) for delta in deltas),
        FinalResult[ChannelOutput](output=full),
        AgentRunResultEvent(AgentRunResult(full)),
    ]


def reply_segments(*, image_file: str | None = "/tmp/octomate-scenario.png") -> list[MessageSegment]:
    segments: list[MessageSegment] = [
        MarkdownSegment(
            data={"text": "## Scenario\nA *markdown* reply segment."},
        ),
        CardSegment(
            data=CardData(payload={"title": "Scenario card", "body": "card payload"}),
        ),
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


def segments_reply(*, image_file: str | None = "/tmp/octomate-scenario.png") -> ChannelScript:
    segments = reply_segments(image_file=image_file)
    return [
        *(ResultSegmentEvent(segment=segment) for segment in segments),
        FinalResult[ChannelOutput](output=segments),
        AgentRunResultEvent(AgentRunResult(segments)),
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


def showcase(*, image_file: str | None = None) -> ChannelScript:
    """Thinking + tools + todos + segment reply — the visual-inspection script."""
    plan, docs = scenario_todos()
    segments = reply_segments(image_file=image_file)
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
        *(ResultSegmentEvent(segment=segment) for segment in segments),
        FinalResult[ChannelOutput](output=segments),
        AgentRunResultEvent(AgentRunResult(segments)),
    ]
