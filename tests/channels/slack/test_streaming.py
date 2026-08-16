"""SlackTentacle streaming and consume: deltas, timeline, segments, todos,
action batches, and assistant-thread bootstrapping."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path
from types import SimpleNamespace
from typing import cast
from uuid import uuid4

import pytest
from pydantic import JsonValue
from pydantic_ai import AgentRunResult, AgentRunResultEvent
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
from pydantic_ai.result import FinalResult
from slack_sdk.models.messages.chunk import Chunk, TaskUpdateChunk

from octomate import Octomate
from octomate.capabilities.harness.events import (
    ResultSegmentEvent,
    StreamEvents,
    SubagentActivity,
    TodoCompletedEvent,
    TodoCreatedEvent,
    TodoStatusChangedEvent,
)
from octomate.schemas.conversation import ChannelAddress
from octomate.schemas.segments import (
    CardData,
    CardSegment,
    ImageData,
    ImageSegment,
    MarkdownSegment,
)
from octomate.schemas.thread import Thread, ThreadKey
from octomate.schemas.todos import Todo
from octomate.tentacles.channels.base import ChannelOutput
from octomate.tentacles.channels.slack.feelers import output as slack_output
from octomate.tentacles.channels.slack.feelers.actions import SlackBlockAction
from octomate.tentacles.channels.slack.feelers.questions import (
    question_choice_block_id,
)
from octomate.types.json import JsonObject
from tests.channels.slack.fakes import (
    FakeSlackInk,
    FakeSlackStream,
    slack_channel,
    slack_key,
)
from tests.support.channels import (
    RecordingDeferredActions,
    drive,
)
from tests.support.scenarios import action_batch, batch_actions, mid_run_notice, play


def _json_object(value: JsonValue) -> JsonObject:
    assert isinstance(value, dict)
    return value


def _json_objects(value: JsonValue) -> list[JsonObject]:
    assert isinstance(value, list)
    objects: list[JsonObject] = []
    for item in value:
        assert isinstance(item, dict)
        objects.append(item)
    return objects


async def test_slack_consume_renders_timeline_per_event() -> None:
    ink = FakeSlackInk()
    channel = slack_channel(ink)

    async def events() -> AsyncIterator[
        StreamEvents[ChannelOutput] | AgentRunResultEvent[ChannelOutput]
    ]:
        yield PartStartEvent(index=0, part=ThinkingPart(content="checking"))
        # ask_questions is skipped from the timeline (call + result).
        yield FunctionToolCallEvent(
            ToolCallPart(tool_name="ask_questions", args={}, tool_call_id="call_q")
        )
        yield FunctionToolResultEvent(
            ToolReturnPart(
                tool_name="ask_questions", content={"ok": True}, tool_call_id="call_q"
            )
        )
        # teleport is internal routing (deferred, no result) — skipped from the
        # timeline; its call id must not surface as a task chunk below.
        yield FunctionToolCallEvent(
            ToolCallPart(
                tool_name="teleport", args={"hint": "x"}, tool_call_id="call_tp"
            )
        )
        yield FunctionToolCallEvent(
            ToolCallPart(tool_name="lookup", args={"query": "x"}, tool_call_id="call_1")
        )
        yield FunctionToolResultEvent(
            ToolReturnPart(
                tool_name="lookup", content={"ok": True}, tool_call_id="call_1"
            )
        )
        # The answer streams as native text parts.
        yield PartStartEvent(index=1, part=TextPart(content="do"))
        yield PartDeltaEvent(index=1, delta=TextPartDelta(content_delta="ne"))
        yield FinalResult[ChannelOutput](output="done")
        yield AgentRunResultEvent(AgentRunResult("done"))

    message_id = await drive(channel, slack_key(), events())

    assert message_id == "stream-ts"
    # The tasks render in a "plan" stream; the answer is its own text message.
    plan_streams = [s for s in ink.stream_objects if s.chunks]
    text_streams = [s for s in ink.stream_objects if s.appends and not s.chunks]
    assert ink.streams[0]["task_display_mode"] == "plan"
    chunks = [chunk for group in ink.stream_chunks for chunk in group]
    task_chunks = [chunk for chunk in chunks if isinstance(chunk, TaskUpdateChunk)]
    # ask_questions skipped; thinking + lookup rendered.
    assert {chunk.id for chunk in task_chunks} == {"thinking-1", "call_1"}
    assert any(chunk.title == "Thinking" for chunk in task_chunks)
    assert any(chunk.title == "Lookup" for chunk in task_chunks)
    details = "\n\n".join(chunk.details or "" for chunk in task_chunks)
    assert "ask_questions" not in details
    # Slack appends a task's details across chunks, so a task's chunks have to
    # concatenate back into its text — each section written exactly once.
    thinking_written = [c for c in task_chunks if c.id == "thinking-1"]
    assert "".join(c.details or "" for c in thinking_written) == "checking"
    assert thinking_written[-1].status == "complete"
    tool_written = "".join(c.details or "" for c in task_chunks if c.id == "call_1")
    assert tool_written.count("*Arguments*") == 1
    assert tool_written.count("*Result*") == 1
    # The answer "done" lands in its own text stream, not the plan stream.
    assert len(plan_streams) == 1
    assert "".join(text_streams[-1].appends) == "done"
    assert "".join(ink.appends) == "done"
    assert ink.statuses[0] == "Thinking…"
    assert "Lookup…" in ink.statuses
    assert "Writing the response…" in ink.statuses
    assert ink.statuses[-1] == ""


async def test_slack_thinking_appends_coalesce_off_the_drive_loop() -> None:
    # A live thinking re-render runs on a background flush task, so deltas that
    # arrive while a (slow) appendStream is in flight coalesce into the next
    # append instead of blocking the drive loop; the step still folds with the
    # full text.
    class SlowFirstThinkingInk(FakeSlackInk):
        def __init__(self) -> None:
            super().__init__()
            self.thinking_started = asyncio.Event()
            self.release = asyncio.Event()
            self.blocked = False

        async def append_stream_chunks(
            self, stream: FakeSlackStream, chunks: list[Chunk]
        ) -> None:
            chunk = chunks[0]
            if (
                not self.blocked
                and isinstance(chunk, TaskUpdateChunk)
                and chunk.details
                and "checking" in chunk.details
            ):
                self.blocked = True
                self.thinking_started.set()
                await self.release.wait()
            await super().append_stream_chunks(stream, chunks)

    ink = SlowFirstThinkingInk()
    channel = slack_channel(ink)

    async def events() -> AsyncIterator[
        StreamEvents[ChannelOutput] | AgentRunResultEvent[ChannelOutput]
    ]:
        yield PartStartEvent(index=0, part=ThinkingPart(content="checking"))
        # The first live append ("checking") is now in flight on the flush task.
        await ink.thinking_started.wait()
        yield PartDeltaEvent(index=0, delta=ThinkingPartDelta(content_delta=" the"))
        yield PartDeltaEvent(index=0, delta=ThinkingPartDelta(content_delta=" docs"))
        ink.release.set()
        yield PartStartEvent(index=1, part=TextPart(content="done"))
        yield FinalResult[ChannelOutput](output="done")
        yield AgentRunResultEvent(AgentRunResult("done"))

    await drive(channel, slack_key(), events())

    thinking_chunks = [
        chunk
        for group in ink.stream_chunks
        for chunk in group
        if isinstance(chunk, TaskUpdateChunk) and chunk.id == "thinking-1"
    ]
    live = [
        chunk.details
        for chunk in thinking_chunks
        if chunk.status == "in_progress" and chunk.details
    ]
    # One live append ("checking"): the " the"/" docs" deltas that arrived while it
    # was in flight coalesced (no per-delta blocking append) and land in the folded
    # completion rather than another live append.
    assert live == ["checking"]
    # It folds into a "Thought for …" task carrying only what Slack has not been
    # sent yet: appended to the live one, that is the thinking block, written once.
    assert thinking_chunks[-1].status == "complete"
    assert thinking_chunks[-1].details == " the docs"
    assert "".join(c.details or "" for c in thinking_chunks) == "checking the docs"
    assert thinking_chunks[-1].title.startswith("Thought for")


async def test_slack_consume_renders_image_and_card_segments(
    tmp_path: Path,
) -> None:
    ink = FakeSlackInk()
    channel = slack_channel(ink)
    image_path = tmp_path / "reef.png"
    image_path.write_bytes(b"png-bytes")

    async def events() -> AsyncIterator[
        StreamEvents[ChannelOutput] | AgentRunResultEvent[ChannelOutput]
    ]:
        yield ResultSegmentEvent(segment=MarkdownSegment(data={"text": "see:"}))
        yield ResultSegmentEvent(
            segment=ImageSegment(data=ImageData(file=str(image_path)))
        )
        yield ResultSegmentEvent(
            segment=CardSegment(
                data=CardData(payload={"blocks": [{"type": "divider"}]})
            )
        )

    await drive(channel, slack_key(), events())

    # Markdown streams as answer text; the image uploads shared into the thread;
    # the card posts as a blocks message.
    assert ink.appends == ["see:"]
    assert ink.uploads == [("C1", b"png-bytes", "reef.png", "1710000000.000100")]
    sent_channel, _chat_type, messages, reply_to = ink.sent[0]
    assert sent_channel == "C1"
    assert reply_to == "1710000000.000100"
    assert messages[0].blocks == [{"type": "divider"}]


async def test_slack_consume_renders_todos_as_timeline_tasks() -> None:
    ink = FakeSlackInk()
    channel = slack_channel(ink)
    todo = Todo(conversation_id=uuid4(), ref="T1", content="Find the docs")

    async def events() -> AsyncIterator[
        StreamEvents[ChannelOutput] | AgentRunResultEvent[ChannelOutput]
    ]:
        yield TodoCreatedEvent(todo=todo)
        yield TodoStatusChangedEvent(
            todo=todo.model_copy(
                update={"status": "in_progress", "active_form": "Finding the docs"}
            ),
            previous=todo,
        )
        yield TodoCompletedEvent(todo=todo.model_copy(update={"status": "completed"}))

    await drive(channel, slack_key(), events())

    chunks = [chunk for group in ink.stream_chunks for chunk in group]
    todo_chunks = [
        chunk
        for chunk in chunks
        if isinstance(chunk, TaskUpdateChunk) and chunk.id == "todo-T1"
    ]
    assert [chunk.status for chunk in todo_chunks] == [
        "pending",
        "in_progress",
        "complete",
    ]
    # The spinner shows the active form; done shows the content again.
    assert todo_chunks[1].title == "Finding the docs"
    assert todo_chunks[2].title == "Find the docs"


async def test_slack_consume_renders_action_batch_blocks() -> None:
    ink = FakeSlackInk()
    actions = RecordingDeferredActions()
    channel = slack_channel(ink, deferred_actions=actions)
    # The slack block builders serialize the batch id into the buttons, so the
    # scripted actions need one (the action manager sets it on real runs).
    batch_id = uuid4()
    question, approval = batch_actions()
    question = question.model_copy(update={"batch_id": batch_id})
    approval = approval.model_copy(update={"batch_id": batch_id})

    await drive(
        channel,
        slack_key(),
        play(action_batch(questions=[question], approvals=[approval])),
    )

    # The question renders first as a block-kit form in the source thread...
    question_chat, _, question_messages, question_reply_to = ink.sent[0]
    assert question_chat == "C1"
    assert question_reply_to == "1710000000.000100"
    question_msg = question_messages[0]
    assert question_msg.text == "Octomate needs 1 question answered"
    assert question_msg.blocks is not None
    choice_block = next(
        block
        for block in question_msg.blocks
        if block.get("block_id") == question_choice_block_id(question)
    )
    assert _json_object(choice_block["label"])["text"] == (
        "Which option should I take?"
    )
    choice_options = _json_objects(_json_object(choice_block["element"])["options"])
    assert [_json_object(option["text"])["text"] for option in choice_options] == [
        "A",
        "B",
    ]
    question_buttons = _json_objects(question_msg.blocks[-1]["elements"])
    assert question_buttons[0]["action_id"] == (
        SlackBlockAction.ASK_QUESTION_SUBMIT.value
    )
    # ...then the approval card with its approve/deny buttons.
    approval_msg = ink.sent[1][2][0]
    assert approval_msg.text == "Octomate needs 1 approval"
    assert approval_msg.blocks is not None
    request_text = _json_object(approval_msg.blocks[1]["text"])["text"]
    assert isinstance(request_text, str)
    assert "*Permission Required: `deploy`*" in request_text
    approval_buttons = _json_objects(approval_msg.blocks[-1]["elements"])
    assert approval_buttons[0]["action_id"] == (SlackBlockAction.APPROVAL_APPROVE.value)
    # Each presented action is marked with its platform message id.
    marked = dict(actions.marked)
    assert len(actions.marked) == 2
    assert marked[question.id] == "fallback-ts"
    assert marked[approval.id] == "fallback-ts"


async def test_slack_tentacle_ensures_assistant_thread() -> None:
    class FakeThreads:
        def __init__(self) -> None:
            self.calls: list[ChannelAddress | ThreadKey] = []

        async def ensure(
            self,
            address_or_key: ChannelAddress | ThreadKey,
        ) -> Thread:
            self.calls.append(address_or_key)
            return cast(Thread, SimpleNamespace())

    threads = FakeThreads()
    channel = slack_channel(FakeSlackInk())
    channel.octomate = cast(Octomate, SimpleNamespace(thread_manager=threads))

    await channel.on_assistant_thread_started(
        {
            "type": "assistant_thread_started",
            "assistant_thread": {
                "user_id": "U1",
                "channel_id": "D1",
                "thread_ts": "1710000000.000100",
                "context": {"channel_id": "C1", "team_id": "T1"},
            },
            "event_ts": "1710000000.000200",
        }
    )

    assert threads.calls == [
        ChannelAddress(
            channel_tentacle_id="slack",
            chat_type="thread",
            chat_id="D1",
            user_id="U1",
            channel_thread_id="1710000000.000100",
        )
    ]


async def test_slack_timeline_alternates_plan_and_message() -> None:
    ink = FakeSlackInk()
    channel = slack_channel(ink)

    message_id = await drive(channel, slack_key(), play(mid_run_notice()))

    assert message_id == "stream-ts"
    # The run alternates: plan block, notice message, plan block, answer message.
    plan_streams = [s for s in ink.stream_objects if s.chunks]
    text_streams = [s for s in ink.stream_objects if s.appends and not s.chunks]
    assert len(plan_streams) == 2
    assert len(text_streams) == 2
    first_plan, second_plan = plan_streams
    notice, answer = text_streams

    # The notice and the final answer are each their own message, not folded
    # into a plan widget.
    assert "trying another way" in "".join(notice.appends)
    assert "pinning the dependency" in "".join(answer.appends)

    # The in-flight lookup finished — folded — in the first plan it started in,
    # which then closed once it drained.
    first_tasks = [
        chunk
        for group in first_plan.chunks
        for chunk in group
        if isinstance(chunk, TaskUpdateChunk)
    ]
    assert any(
        chunk.id == "call_slow_1" and chunk.status == "complete"
        for chunk in first_tasks
    )
    assert first_plan.stopped

    # The second round (thinking + read_logs) opened a fresh plan below the
    # notice message.
    second_tasks = [
        chunk
        for group in second_plan.chunks
        for chunk in group
        if isinstance(chunk, TaskUpdateChunk)
    ]
    assert {chunk.id for chunk in second_tasks} == {"thinking-2", "call_logs_1"}
    assert second_plan.stopped


async def test_slack_answer_deltas_coalesce_while_append_is_in_flight() -> None:
    class SlowFirstAppendInk(FakeSlackInk):
        def __init__(self) -> None:
            super().__init__()
            self.append_started = asyncio.Event()
            self.release_append = asyncio.Event()
            self.append_calls = 0

        async def append_stream(
            self, stream: FakeSlackStream, markdown_text: str
        ) -> None:
            self.append_calls += 1
            if self.append_calls == 1:
                self.append_started.set()
                await self.release_append.wait()
            await super().append_stream(stream, markdown_text)

    ink = SlowFirstAppendInk()
    channel = slack_channel(ink)

    async def events() -> AsyncIterator[
        StreamEvents[ChannelOutput] | AgentRunResultEvent[ChannelOutput]
    ]:
        yield PartStartEvent(index=0, part=TextPart(content="a"))
        await ink.append_started.wait()
        yield PartDeltaEvent(index=0, delta=TextPartDelta(content_delta="b"))
        yield PartDeltaEvent(index=0, delta=TextPartDelta(content_delta="c"))
        ink.release_append.set()
        yield FinalResult[ChannelOutput](output="abc")
        yield AgentRunResultEvent(AgentRunResult("abc"))

    message_id = await drive(channel, slack_key(), events())

    assert message_id == "stream-ts"
    text_streams = [stream for stream in ink.stream_objects if stream.appends]
    assert len(text_streams) == 1
    assert text_streams[0].appends == ["a", "bc"]


async def test_slack_text_stream_rotates_before_slack_expires(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = [1000.0]
    monkeypatch.setattr(slack_output.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(slack_output, "TEXT_STREAM_ROTATE_AFTER", 10.0)
    ink = FakeSlackInk()
    channel = slack_channel(ink)

    async def events() -> AsyncIterator[
        StreamEvents[ChannelOutput] | AgentRunResultEvent[ChannelOutput]
    ]:
        yield PartStartEvent(index=0, part=TextPart(content="hello"))
        await asyncio.sleep(0)
        now[0] = 1011.0
        yield PartDeltaEvent(index=0, delta=TextPartDelta(content_delta=" again"))
        yield FinalResult[ChannelOutput](output="hello again")
        yield AgentRunResultEvent(AgentRunResult("hello again"))

    message_id = await drive(channel, slack_key(), events())

    assert message_id == "stream-ts"
    assert len(ink.stream_objects) == 2
    assert ink.stream_objects[0].appends == ["hello"]
    assert ink.stream_objects[0].stopped
    assert ink.stream_objects[1].appends == [" again"]


async def test_slack_subagents_own_streams_separate_from_parent_and_siblings() -> None:
    ink = FakeSlackInk()
    channel = slack_channel(ink)

    first_activity = SubagentActivity("call-a", "commission", "audit")
    second_activity = SubagentActivity("call-b", "commission", "tests")
    third_activity = SubagentActivity("call-c", "commission", "docs")
    async with channel.feelers.timeline.open(slack_key()) as parent:
        await parent.thinking_start()
        await parent.thinking_delta("parent work")
        async with (
            parent.open_subagent(first_activity) as first,
            parent.open_subagent(second_activity) as second,
            parent.open_subagent(third_activity) as third,
        ):
            await first.append_response("audit result")
            await second.append_response("test result")
            await third.append_response("docs result")
            await first.settle("completed")
            await second.settle("failed", "one failure")
            await third.settle("completed")
    parent_stream = ink.stream_objects[0]

    assert len(ink.stream_objects) == 4
    first_stream, second_stream, third_stream = ink.stream_objects[1:]
    assert (
        len({id(parent_stream), id(first_stream), id(second_stream), id(third_stream)})
        == 4
    )
    parent_ids = {
        chunk.id
        for group in parent_stream.chunks
        for chunk in group
        if isinstance(chunk, TaskUpdateChunk)
    }
    first_ids = {
        chunk.id
        for group in first_stream.chunks
        for chunk in group
        if isinstance(chunk, TaskUpdateChunk)
    }
    second_ids = {
        chunk.id
        for group in second_stream.chunks
        for chunk in group
        if isinstance(chunk, TaskUpdateChunk)
    }
    third_ids = {
        chunk.id
        for group in third_stream.chunks
        for chunk in group
        if isinstance(chunk, TaskUpdateChunk)
    }
    assert parent_ids == {"thinking-1"}
    assert first_ids == {"call-a"}
    assert second_ids == {"call-b"}
    assert third_ids == {"call-c"}
    assert "audit result" in str(first_stream.chunks)
    assert "test result" not in str(first_stream.chunks)
    assert "test result" in str(second_stream.chunks)
    assert "docs result" in str(third_stream.chunks)
    assert all(stream.stopped for stream in ink.stream_objects)


async def test_actions_presented_folds_the_surface_and_sets_waiting() -> None:
    """An in-process agent bridge presented cards while the run stream is
    live: the thinking spinner completes and the assistant status says the run
    waits on the human, instead of both outliving the parked work."""
    ink = FakeSlackInk()
    channel = slack_channel(ink)

    async with channel.feelers.timeline.open(slack_key()) as state:
        await state.thinking_start()
        await state.actions_presented()

        # Status hints are fire-and-forget tasks; let them land before looking.
        assert isinstance(state, slack_output.SlackTimelineState)
        if state.status_tasks:
            await asyncio.gather(*state.status_tasks)
        assert ink.statuses[-1] == slack_output.STATUS_WAITING
        [plan] = ink.stream_objects
        assert plan.stopped
        task_chunks = [
            chunk
            for chunks in ink.stream_chunks
            for chunk in chunks
            if isinstance(chunk, TaskUpdateChunk)
        ]
        assert task_chunks[-1].status == "complete"
