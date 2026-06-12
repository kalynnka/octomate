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
        *(ResultTextDeltaEvent(delta=delta) for delta in answer_deltas),
        FinalResult[ChannelOutput](output=answer),
        AgentRunResultEvent(AgentRunResult(answer)),
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
