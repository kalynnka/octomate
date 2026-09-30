"""Permission callbacks must not close a surface while an event renders to it."""

import asyncio
from collections.abc import AsyncIterator
from typing import Literal
from unittest.mock import AsyncMock

import pytest
from arcanus import RelationCollection
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
from pydantic_ai.tools import DeferredToolRequests
from slack_sdk.models.messages.chunk import Chunk, PlanUpdateChunk, TaskUpdateChunk
from uuid_utils.compat import uuid7

from octomate.capabilities.harness.events import StreamEvents
from octomate.config import DiscordStreamConfig
from octomate.managers.deferred import DeferredActionManager
from octomate.schemas.conversation import Conversation
from octomate.schemas.deferred import (
    DeferredActionBatch,
    DeferredActionCollection,
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
    SlowFirstEditInk,
    discord_address,
    discord_channel,
)
from tests.channels.slack.fakes import (
    FakeSlackInk,
    FakeSlackStream,
    slack_channel,
    slack_key,
)
from tests.channels.test_feelers import plain_feelers, present_one_approval
from tests.support.channels import NoopTimeline


class OpeningPlanInk(FakeSlackInk):
    def __init__(self) -> None:
        super().__init__()
        self.header_started = asyncio.Event()
        self.release_header = asyncio.Event()

    async def append_stream_chunks(
        self, stream: FakeSlackStream, chunks: list[Chunk]
    ) -> None:
        if isinstance(chunks[0], PlanUpdateChunk):
            self.header_started.set()
            await self.release_header.wait()
        assert not stream.stopped
        await super().append_stream_chunks(stream, chunks)


async def test_slack_permission_during_plan_creation_preserves_the_answer() -> None:
    ink = OpeningPlanInk()
    channel = slack_channel(ink)
    address = slack_key()
    presenting = asyncio.Event()
    presented = asyncio.Event()

    async def events() -> AsyncIterator[StreamEvents[ChannelOutput]]:
        yield FunctionToolCallEvent(
            ToolCallPart(tool_name="lookup", args={}, tool_call_id="lookup-1")
        )
        await presented.wait()
        yield FunctionToolResultEvent(
            ToolReturnPart(tool_name="lookup", content="found", tool_call_id="lookup-1")
        )
        yield PartStartEvent(index=0, part=TextPart(content="Complete answer"))
        yield PartEndEvent(index=0, part=TextPart(content="Complete answer"))

    async def present() -> None:
        presenting.set()
        await present_one_approval(channel.feelers, address)
        presented.set()

    async with asyncio.timeout(3), channel.feelers.timeline.open(address) as state:
        async with channel.feelers.driving(address, state):
            async with asyncio.TaskGroup() as tasks:
                tasks.create_task(state.drive(events()))
                await ink.header_started.wait()
                tasks.create_task(present())
                await presenting.wait()
                ink.release_header.set()

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
    assert [task.status for task in updates] == ["in_progress", "complete"]
    assert plan.stopped
    assert answer.stopped


async def test_discord_permission_flush_serializes_arriving_text() -> None:
    ink = SlowFirstEditInk()
    channel = discord_channel(
        ink, DiscordStreamConfig(enabled=True, min_chars=1000, flush_interval=60)
    )
    address = discord_address()
    initial_text = asyncio.Event()
    offer_delta = asyncio.Event()
    delta_offered = asyncio.Event()
    presented = asyncio.Event()

    async def events() -> AsyncIterator[StreamEvents[ChannelOutput]]:
        yield PartStartEvent(index=0, part=TextPart(content="Before"))
        initial_text.set()
        await offer_delta.wait()
        delta_offered.set()
        yield PartDeltaEvent(index=0, delta=TextPartDelta(content_delta=" after"))
        await presented.wait()
        yield PartEndEvent(index=0, part=TextPart(content="Before after"))

    async def present() -> None:
        await present_one_approval(channel.feelers, address)
        presented.set()

    async with asyncio.timeout(3), channel.feelers.timeline.open(address) as state:
        async with channel.feelers.driving(address, state):
            async with asyncio.TaskGroup() as tasks:
                tasks.create_task(state.drive(events()))
                await initial_text.wait()
                tasks.create_task(present())
                await ink.edit_started.wait()
                offer_delta.set()
                await delta_offered.wait()
                ink.release_edit.set()

    assert ink.max_active_edits == 1
    messages = {edit.message_id: edit.content for edit in ink.edits}
    assert "".join(messages.values()) == "Before after"
    assert len(ink.sends) == 1


@pytest.mark.parametrize("platform", ["slack", "discord"])
async def test_pending_actions_do_not_split_continuing_text(
    platform: Literal["slack", "discord"], monkeypatch: pytest.MonkeyPatch
) -> None:
    if platform == "slack":
        ink = FakeSlackInk()
        channel = slack_channel(ink)
        channel.config.stream.flush_interval = 60
        channel.config.stream.min_chars = 1000
        address = slack_key()
    else:
        ink = RecordingDiscordInk()
        channel = discord_channel(
            ink, DiscordStreamConfig(enabled=True, flush_interval=60, min_chars=1000)
        )
        address = discord_address()
    conversation = Conversation(thread_id=uuid7(), agent_tentacle_id="codex")
    requests = [
        DeferredToolRequests(
            approvals=[ToolCallPart("shell", {"cmd": "pwd"}, "approval-1")]
        ),
        DeferredToolRequests(
            calls=[
                ToolCallPart(
                    "ask_questions",
                    {"questions": [{"question": "Which branch?"}]},
                    "question-1",
                )
            ]
        ),
    ]
    batches: list[DeferredActionBatch] = []
    for request in requests:
        actions = DeferredActionCollection.validate_python(request)
        batch_id = uuid7()
        for action in actions:
            action.batch_id = batch_id
        batch = DeferredActionBatch(
            id=batch_id,
            conversation_id=conversation.id,
            agent_tentacle_id="codex",
            source_address=address,
            target_address=address,
            target_mode="main",
            requests=request,
            questions=RelationCollection(
                [action for action in actions if isinstance(action, DeferredQuestion)]
            ),
            approvals=RelationCollection(
                [action for action in actions if isinstance(action, DeferredApproval)]
            ),
        )
        batches.append(batch)
    manager = DeferredActionManager()
    monkeypatch.setattr(manager, "create_batch", AsyncMock(side_effect=batches))
    marked = AsyncMock()
    monkeypatch.setattr(manager, "mark_action_presented", marked)
    initial_text = asyncio.Event()
    presented = asyncio.Event()

    async def present(request: DeferredToolRequests) -> None:
        await channel.feelers.present_actions(
            action_manager=manager,
            conversation=conversation,
            agent_tentacle_id="codex",
            run_name="react",
            source_address=address,
            target_address=address,
            target_mode="main",
            decision=None,
            requests=request,
        )

    async def events() -> AsyncIterator[StreamEvents[ChannelOutput]]:
        yield PartStartEvent(index=0, part=TextPart(content="Before"))
        yield PartDeltaEvent(index=0, delta=TextPartDelta(content_delta=" buffered"))
        initial_text.set()
        await presented.wait()
        yield PartDeltaEvent(index=0, delta=TextPartDelta(content_delta=" after"))
        yield PartEndEvent(index=0, part=TextPart(content="Before buffered after"))

    async with asyncio.timeout(3), channel.feelers.timeline.open(address) as state:
        async with (
            channel.feelers.driving(address, state),
            asyncio.TaskGroup() as tasks,
        ):
            tasks.create_task(state.drive(events()))
            await initial_text.wait()
            await asyncio.gather(*(present(request) for request in requests))
            if isinstance(ink, FakeSlackInk):
                assert "".join(ink.appends) == "Before buffered"
            else:
                assert ink.edits[-1].content == "Before buffered"
            presented.set()

    assert marked.await_count == 2
    assert all(batch.status == "pending" for batch in batches)
    assert all(not batch.completed for batch in batches)
    if isinstance(ink, FakeSlackInk):
        assert len(ink.stream_objects) == 1
        assert ink.stream_objects[0].stopped
        assert "".join(ink.appends) == "Before buffered after"
    else:
        assert len(ink.sends) == 1
        assert ink.edits[-1].content == "Before buffered after"


@pytest.mark.parametrize("cancel", [False, True])
async def test_timeline_waits_for_permission_settlement_before_cleanup(
    cancel: bool,
) -> None:
    settling = asyncio.Event()
    release_settle = asyncio.Event()
    leave = asyncio.Event()
    leaving = asyncio.Event()
    finished: list[str] = []

    class WaitingTimeline(NoopTimeline):
        async def actions_presented(self) -> None:
            settling.set()
            await release_settle.wait()
            finished.append("settled")

    feelers = plain_feelers()
    address = slack_key()
    timeline = WaitingTimeline()
    registered = asyncio.Event()

    async def run() -> None:
        try:
            async with feelers.driving(address, timeline):
                registered.set()
                try:
                    await leave.wait()
                finally:
                    leaving.set()
        finally:
            finished.append("closed")

    async with asyncio.timeout(3), asyncio.TaskGroup() as tasks:
        driver = tasks.create_task(run())
        await registered.wait()
        tasks.create_task(present_one_approval(feelers, address))
        await settling.wait()
        if cancel:
            driver.cancel()
        else:
            leave.set()
        await leaving.wait()
        assert not finished
        release_settle.set()

    assert finished == ["settled", "closed"]
    assert not feelers.live_timelines


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
