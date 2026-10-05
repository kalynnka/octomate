"""A batch presented mid-run must not close a surface the run still renders to."""

import asyncio
from collections.abc import AsyncIterator
from typing import Literal, cast
from unittest.mock import AsyncMock

import pytest
from pydantic_ai.messages import (
    FunctionToolCallEvent,
    FunctionToolResultEvent,
    PartDeltaEvent,
    PartEndEvent,
    PartStartEvent,
    TextPart,
    TextPartDelta,
    ToolCallPart,
    ToolReturnPart,
)
from slack_sdk.models.messages.chunk import TaskUpdateChunk
from uuid_utils.compat import uuid7

from octomate.capabilities.harness.events import ActionBatchEvent, StreamEvents
from octomate.config import DiscordStreamConfig
from octomate.managers.deferred import DeferredActionManager
from octomate.schemas.deferred import (
    ApprovalRequest,
    DeferredApproval,
    DeferredQuestion,
)
from octomate.tentacles.channel import ChannelOutput
from octomate.tentacles.slack.feelers.output import (
    TEXT_STREAM_ROTATE_AFTER,
    SlackTimelineState,
)
from tests.channels.discord.test_streaming import (
    RecordingDiscordInk,
    discord_address,
    discord_channel,
)
from tests.channels.slack.fakes import (
    FakeSlackInk,
    FakeSlackStream,
    slack_channel,
    slack_key,
)
from tests.support.managers import FakeActionManager
from tests.support.scenarios import play


def approval_batch() -> ActionBatchEvent:
    batch_id = uuid7()
    return ActionBatchEvent(
        batch_id=str(batch_id),
        approvals=[
            DeferredApproval(
                batch_id=batch_id,
                tool_name="shell",
                tool_call_id="approval-1",
                args=ApprovalRequest(tool_name="shell", args={"cmd": "pwd"}),
            )
        ],
    )


async def test_slack_batch_during_a_plan_preserves_the_answer() -> None:
    ink = FakeSlackInk()
    channel = slack_channel(ink, cast(DeferredActionManager, FakeActionManager()))
    address = slack_key()
    events: list[StreamEvents[ChannelOutput]] = [
        FunctionToolCallEvent(
            ToolCallPart(tool_name="lookup", args={}, tool_call_id="lookup-1")
        ),
        approval_batch(),
        FunctionToolResultEvent(
            ToolReturnPart(tool_name="lookup", content="found", tool_call_id="lookup-1")
        ),
        PartStartEvent(index=0, part=TextPart(content="Complete answer")),
        PartEndEvent(index=0, part=TextPart(content="Complete answer")),
    ]

    async with asyncio.timeout(3), channel.feelers.timeline.open(address) as state:
        await state.drive(play(events))

    assert "".join(ink.appends) == "Complete answer"
    assert len(ink.stream_objects) == 2
    assert len(ink.stops) == 2
    plan, answer = ink.stream_objects
    updates = [
        chunk
        for chunks in plan.chunks
        for chunk in chunks
        if isinstance(chunk, TaskUpdateChunk)
    ]
    # The tool result lands on the entry the call opened, past the batch.
    assert [task.status for task in updates] == ["in_progress", "complete"]
    assert plan.stopped
    assert answer.stopped


@pytest.mark.parametrize("platform", ["slack", "discord"])
async def test_pending_actions_do_not_split_continuing_text(
    platform: Literal["slack", "discord"],
) -> None:
    marked = AsyncMock()
    if platform == "slack":
        ink = FakeSlackInk()
        manager = FakeActionManager()
        channel = slack_channel(ink, cast(DeferredActionManager, manager))
        channel.config.stream.flush_interval = 60
        channel.config.stream.min_chars = 1000
        address = slack_key()
    else:
        ink = RecordingDiscordInk()
        channel = discord_channel(
            ink, DiscordStreamConfig(enabled=True, flush_interval=60, min_chars=1000)
        )
        channel.octomate.deferred_actions.mark_action_presented = marked
        address = discord_address()
    question_batch_id = uuid7()
    question = DeferredQuestion(
        batch_id=question_batch_id,
        tool_name="ask_questions",
        tool_call_id="question-1",
        args={"question": "Which branch?"},
    )
    flushed: list[str] = []

    def drawn() -> str:
        if isinstance(ink, FakeSlackInk):
            return "".join(ink.appends)
        return ink.edits[-1].content

    async def events() -> AsyncIterator[StreamEvents[ChannelOutput]]:
        yield PartStartEvent(index=0, part=TextPart(content="Before"))
        yield PartDeltaEvent(index=0, delta=TextPartDelta(content_delta=" buffered"))
        yield approval_batch()
        yield ActionBatchEvent(batch_id=str(question_batch_id), questions=[question])
        # Both batches are up: what streamed so far shows, and the message is open.
        flushed.append(drawn())
        yield PartDeltaEvent(index=0, delta=TextPartDelta(content_delta=" after"))
        yield PartEndEvent(index=0, part=TextPart(content="Before buffered after"))

    async with asyncio.timeout(3), channel.feelers.timeline.open(address) as state:
        await state.drive(events())

    assert flushed == ["Before buffered"]
    if isinstance(ink, FakeSlackInk):
        assert len(manager.presented) == 2
        assert len(ink.stream_objects) == 1
        assert ink.stream_objects[0].stopped
    else:
        assert marked.await_count == 2
        assert len(ink.sends) == 1
    assert drawn() == "Before buffered after"


async def test_slack_text_arriving_during_rotation_uses_the_replacement_stream() -> (
    None
):
    class ClosingTextInk(FakeSlackInk):
        def __init__(self) -> None:
            super().__init__()
            self.closing = asyncio.Event()
            self.release_close = asyncio.Event()

        async def stop_stream(
            self, stream: FakeSlackStream, *, markdown_text: str | None = None
        ) -> str:
            if stream is self.stream_objects[0] and not self.closing.is_set():
                self.closing.set()
                await self.release_close.wait()
            return await super().stop_stream(stream, markdown_text=markdown_text)

    ink = ClosingTextInk()
    channel = slack_channel(ink)
    async with asyncio.timeout(3), channel.feelers.timeline.open(slack_key()) as state:
        assert isinstance(state, SlackTimelineState)
        await state.answer_delta("first")
        await state.text_flusher.drain()
        await state.flush_text()
        state.text_stream_started_at -= TEXT_STREAM_ROTATE_AFTER
        await state.answer_delta(" second")
        await ink.closing.wait()
        await state.answer_delta(" third")
        ink.release_close.set()

    assert "".join(ink.appends) == "first second third"
    assert len(ink.stream_objects) == 2
    assert all(stream.stopped for stream in ink.stream_objects)
