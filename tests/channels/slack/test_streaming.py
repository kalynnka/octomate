"""SlackTentacle streaming and consume: deltas, timeline, segments, todos,
action batches, and assistant-thread bootstrapping."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from types import SimpleNamespace
from typing import cast
from uuid import uuid4

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
    ToolCallPart,
    ToolReturnPart,
)
from pydantic_ai.result import FinalResult
from pydantic_ai.tools import DeferredToolRequests
from slack_sdk.models.messages.chunk import TaskUpdateChunk

from octomate import Octomate
from octomate.capabilities.events import (
    ResultSegmentEvent,
    StreamEvents,
    TodoCompletedEvent,
    TodoCreatedEvent,
    TodoStatusChangedEvent,
)
from octomate.config import SlackStreamConfig
from octomate.schemas.conversation import Conversation, ConversationKey
from octomate.schemas.segments import (
    CardData,
    CardSegment,
    ImageData,
    ImageSegment,
    MarkdownSegment,
)
from octomate.schemas.todos import Todo
from octomate.tentacles.channel.base import ChannelOutput
from octomate.tentacles.channel.slack.feelers.actions import SlackBlockAction
from octomate.tentacles.channel.slack.feelers.questions import (
    question_choice_block_id,
)
from octomate.types.json import JsonObject

from tests.channels.slack.fakes import (
    FakeSlackInk,
    FakeSlackStream,
    compose_slack_feelers,
    slack_channel,
    slack_key,
)
from tests.support.channels import (
    RecordingDeferredActions,
    output_events,
    streamed_result,
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


async def test_slack_tentacle_streams_text_deltas_in_source_thread() -> None:
    ink = FakeSlackInk()
    channel = slack_channel(ink)

    await channel.feelers.markdown_stream.present(
        slack_key(),
        streamed_result("# Hello\n**world**", "# Hello\n", "**world**"),
    )

    assert ink.streams == [
        {
            "channel": "C1",
            "thread_ts": "1710000000.000100",
            "recipient_user_id": "U1",
            "recipient_team_id": None,
        }
    ]
    assert ink.appends == ["# Hello\n", "**world**"]
    assert ink.stops == [None]
    assert ink.finals == []


async def test_slack_present_output_renders_output_event_deltas() -> None:
    ink = FakeSlackInk()
    channel = slack_channel(ink)

    await channel.feelers.markdown_stream.present_output(
        slack_key(),
        output_events("# Hello\n", "**world**"),
    )

    assert ink.appends == ["# Hello\n", "**world**"]
    assert ink.stops == [None]


async def test_slack_tentacle_can_batch_stream_deltas_when_configured() -> None:
    ink = FakeSlackInk()
    channel = slack_channel(ink)
    channel.config.stream = SlackStreamConfig(flush_interval=999, min_chars=100)
    compose_slack_feelers(channel)

    await channel.feelers.markdown_stream.present(
        slack_key(),
        streamed_result("# Hello\n**world**", "# Hello\n", "**world**"),
    )

    assert ink.appends == ["# Hello\n**world**"]
    assert ink.stops == [None]


async def test_slack_tentacle_stops_stream_on_deferred_result() -> None:
    ink = FakeSlackInk()
    channel = slack_channel(ink)

    await channel.feelers.markdown_stream.present(
        slack_key(),
        streamed_result(
            DeferredToolRequests(
                calls=[
                    ToolCallPart(
                        tool_name="ask_user",
                        args={"question": "Continue?"},
                        tool_call_id="call_1",
                    )
                ]
            )
        ),
    )

    assert ink.appends == []
    assert ink.sent == []


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

    message_id = await channel.consume(slack_key(), events())

    assert message_id == "stream-ts"
    # "plan" gathers all the run's tasks into one plan block.
    assert ink.streams and ink.streams[0]["task_display_mode"] == "plan"
    chunks = [chunk for group in ink.stream_chunks for chunk in group]
    task_chunks = [chunk for chunk in chunks if isinstance(chunk, TaskUpdateChunk)]
    # ask_questions skipped; thinking + lookup rendered.
    assert {chunk.id for chunk in task_chunks} == {"thinking-1", "call_1"}
    assert any(chunk.title == "Thinking" for chunk in task_chunks)
    assert any(chunk.title == "Lookup" for chunk in task_chunks)
    details = "\n\n".join(chunk.details or "" for chunk in task_chunks)
    assert "ask_questions" not in details
    assert "".join(ink.appends) == "done"
    assert ink.stops == ["stream-ts"] or ink.stops == [None]
    assert ink.statuses[0] == "Thinking…"
    assert "Lookup…" in ink.statuses
    assert "Writing the response…" in ink.statuses
    assert ink.statuses[-1] == ""


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
            segment=CardSegment(data=CardData(payload={"blocks": [{"type": "divider"}]}))
        )

    await channel.consume(slack_key(), events())

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

    await channel.consume(slack_key(), events())

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
    channel = slack_channel(ink)
    actions = RecordingDeferredActions()
    channel.octomate = cast(Octomate, SimpleNamespace(deferred_actions=actions))
    # The slack block builders serialize the batch id into the buttons, so the
    # scripted actions need one (the action manager sets it on real runs).
    batch_id = uuid4()
    question, approval = batch_actions()
    question = question.model_copy(update={"batch_id": batch_id})
    approval = approval.model_copy(update={"batch_id": batch_id})

    await channel.consume(
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
    assert approval_buttons[0]["action_id"] == (
        SlackBlockAction.APPROVAL_APPROVE.value
    )
    # Each presented action is marked with its platform message id.
    marked = {action_id: message_id for action_id, message_id in actions.marked}
    assert len(actions.marked) == 2
    assert marked[question.id] == "fallback-ts"
    assert marked[approval.id] == "fallback-ts"


async def test_slack_tentacle_streams_final_only_result_once() -> None:
    ink = FakeSlackInk()
    channel = slack_channel(ink)

    await channel.feelers.markdown_stream.present(
        slack_key(),
        streamed_result("final **markdown**"),
    )

    assert len(ink.streams) == 1
    assert ink.appends == ["final **markdown**"]
    assert ink.stops == [None]
    assert ink.finals == []


async def test_slack_tentacle_closes_open_stream_on_append_error() -> None:
    class RaisingSlackInk(FakeSlackInk):
        async def append_stream(
            self,
            stream: FakeSlackStream,
            markdown_text: str,
        ) -> None:
            await super().append_stream(stream, markdown_text)
            raise RuntimeError("append failed")

    ink = RaisingSlackInk()
    channel = slack_channel(ink)

    await channel.feelers.markdown_stream.present(
        slack_key(),
        streamed_result("hello"),
    )

    assert ink.appends == ["hello"]
    assert ink.stops == [None]


async def test_slack_tentacle_ensures_assistant_thread_conversation() -> None:
    class FakeConversations:
        def __init__(self) -> None:
            self.calls: list[tuple[ConversationKey, str | None]] = []

        async def ensure(
            self,
            key: ConversationKey,
            *,
            agent_tentacle_id: str | None = None,
        ) -> Conversation:
            self.calls.append((key, agent_tentacle_id))
            return cast(Conversation, SimpleNamespace())

    conversations = FakeConversations()
    channel = slack_channel(FakeSlackInk())
    channel.octomate = cast(Octomate, SimpleNamespace(conversations=conversations))
    channel.agent_id = "inkling"

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

    assert len(conversations.calls) == 1
    key, agent_id = conversations.calls[0]
    assert key == ConversationKey(
        channel_tentacle_id="slack",
        chat_type="private",
        chat_id="D1",
        user_id="U1",
        thread_id="1710000000.000100",
    )
    assert agent_id == "inkling"


async def test_slack_timeline_rotates_plan_on_mid_run_notice() -> None:
    ink = FakeSlackInk()
    channel = slack_channel(ink)

    message_id = await channel.consume(slack_key(), play(mid_run_notice()))

    assert message_id == "stream-ts"
    # The notice triggered a rotation: a second plan stream was opened.
    assert len(ink.stream_objects) == 2
    first, second = ink.stream_objects

    # The notice streamed inline into the first plan message...
    assert "trying another way" in "".join(first.appends)
    # ...and the in-flight lookup still completed there after the rotation,
    # which then closed the drained first message.
    first_tasks = [
        chunk
        for group in first.chunks
        for chunk in group
        if isinstance(chunk, TaskUpdateChunk)
    ]
    assert any(
        chunk.id == "call_slow_1" and chunk.status == "complete"
        for chunk in first_tasks
    )
    assert first.stopped

    # The second round (thinking + read_logs) rendered in the new plan message,
    # along with the final answer text.
    second_tasks = [
        chunk
        for group in second.chunks
        for chunk in group
        if isinstance(chunk, TaskUpdateChunk)
    ]
    assert {chunk.id for chunk in second_tasks} == {"thinking-2", "call_logs_1"}
    assert "pinning the dependency" in "".join(second.appends)
    assert second.stopped
