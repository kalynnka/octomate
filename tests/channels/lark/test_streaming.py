"""Lark streaming-card and consume (event-timeline) rendering tests."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from pathlib import Path
from uuid import uuid4

from pydantic import TypeAdapter
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

from octomate.capabilities.harness.events import (
    ResultSegmentEvent,
    StreamEvents,
    SubagentActivity,
    TodoCompletedEvent,
    TodoCreatedEvent,
)
from octomate.schemas.conversation import ChannelAddress
from octomate.schemas.segments import CardData, CardSegment, ImageData, ImageSegment
from octomate.schemas.todos import Todo
from octomate.tentacles.channels.base import ChannelOutput
from octomate.tentacles.channels.lark.schema import LarkStreamCard
from octomate.types.json import JsonObject
from tests.channels.lark.fakes import FakeLarkInk, lark_channel
from tests.support.channels import (
    RecordingDeferredActions,
    drive,
)
from tests.support.scenarios import action_batch, batch_actions, mid_run_notice, play

JsonObjectAdapter = TypeAdapter(JsonObject)


def _loaded_json_object(value: str) -> JsonObject:
    return JsonObjectAdapter.validate_json(value)


async def test_lark_consume_renders_timeline_per_event() -> None:
    ink = FakeLarkInk()
    channel = lark_channel(ink)
    address = ChannelAddress(
        channel_tentacle_id="lark",
        chat_type="dm",
        chat_id="u1",
        user_id="u1",
    )

    async def events() -> AsyncIterator[
        StreamEvents[ChannelOutput] | AgentRunResultEvent[ChannelOutput]
    ]:
        yield PartStartEvent(index=0, part=ThinkingPart(content="checking"))
        yield FunctionToolCallEvent(
            ToolCallPart(tool_name="lookup", args={"query": "x"}, tool_call_id="call_1")
        )
        yield FunctionToolResultEvent(
            ToolReturnPart(
                tool_name="lookup", content={"ok": True}, tool_call_id="call_1"
            )
        )
        # The answer streams as raw text parts (the react graph, not the normalizer).
        yield PartStartEvent(index=1, part=TextPart(content="done"))
        yield AgentRunResultEvent(AgentRunResult("done"))

    message_id = await drive(channel, address, events())

    # Thinking + tool each posted as their own card (then folded via patch)...
    assert len(ink.created) == 2
    assert len(ink.patched) == 2
    # ...and the answer streamed into its own stream card, then finalized.
    assert ink.stream_cards
    assert any("done" in content for _card, content, _seq in ink.stream_updates)
    assert ink.finalized
    assert message_id == "stream-1"


async def test_lark_thinking_patches_coalesce_off_the_drive_loop() -> None:
    # A live thinking re-render runs on a background flush task, so deltas that
    # arrive while a (slow) card patch is in flight coalesce into the next patch
    # instead of blocking the drive loop; the block still folds with the full text.
    class SlowFirstPatchInk(FakeLarkInk):
        def __init__(self) -> None:
            super().__init__()
            self.patch_started = asyncio.Event()
            self.release_patch = asyncio.Event()
            self.patch_calls = 0

        async def patch_card(self, message_id: str, content: str) -> bool:
            self.patch_calls += 1
            if self.patch_calls == 1:
                self.patch_started.set()
                await self.release_patch.wait()
            return await super().patch_card(message_id, content)

    ink = SlowFirstPatchInk()
    channel = lark_channel(ink)
    address = ChannelAddress(
        channel_tentacle_id="lark",
        chat_type="dm",
        chat_id="u1",
        user_id="u1",
    )

    async def events() -> AsyncIterator[
        StreamEvents[ChannelOutput] | AgentRunResultEvent[ChannelOutput]
    ]:
        yield PartStartEvent(index=0, part=ThinkingPart(content="checking"))
        # The first live patch ("checking") is now in flight on the flush task.
        await ink.patch_started.wait()
        yield PartDeltaEvent(index=0, delta=ThinkingPartDelta(content_delta=" the"))
        yield PartDeltaEvent(index=0, delta=ThinkingPartDelta(content_delta=" docs"))
        ink.release_patch.set()
        yield PartStartEvent(index=1, part=TextPart(content="done"))
        yield AgentRunResultEvent(AgentRunResult("done"))

    await drive(channel, address, events())

    live = [content for _id, content in ink.patched if "Thinking…" in content]
    # Exactly one live patch ("checking"): the " the"/" docs" deltas that arrived
    # while it was in flight coalesced (no per-delta blocking patch) and land in
    # the folded panel rather than another live patch.
    assert len(live) == 1
    assert "checking" in live[0]
    assert "docs" not in live[0]
    # It folds into a "Thought for Ns" collapsible panel with the full text.
    folded = [content for _id, content in ink.patched if "Thought for" in content]
    assert folded
    assert "checking the docs" in folded[0]


async def test_lark_consume_renders_image_and_card_segments(
    tmp_path: Path,
) -> None:
    ink = FakeLarkInk()
    channel = lark_channel(ink)
    address = ChannelAddress(
        channel_tentacle_id="lark",
        chat_type="dm",
        chat_id="u1",
        user_id="u1",
    )
    image_path = tmp_path / "reef.png"
    image_path.write_bytes(b"png-bytes")

    async def events() -> AsyncIterator[
        StreamEvents[ChannelOutput] | AgentRunResultEvent[ChannelOutput]
    ]:
        yield ResultSegmentEvent(
            segment=ImageSegment(data=ImageData(file=str(image_path)))
        )
        yield ResultSegmentEvent(
            segment=CardSegment(data=CardData(payload={"header": {"title": "t"}}))
        )

    await drive(channel, address, events())

    # The image uploads then sends as an image message; the card posts as an
    # interactive message carrying the payload verbatim.
    assert ink.uploaded == [b"png-bytes"]
    image_contents = [
        content for _, _, msg_type, content in ink.created if msg_type == "image"
    ]
    assert [json.loads(content) for content in image_contents] == [
        {"image_key": "img-address-1"}
    ]
    interactive_contents = [
        content for _, _, msg_type, content in ink.created if msg_type == "interactive"
    ]
    assert json.loads(interactive_contents[-1]) == {"header": {"title": "t"}}


async def test_lark_card_segment_falls_back_to_raw_text_when_card_fails() -> None:
    ink = FakeLarkInk()
    ink.fail_interactive_send = True
    channel = lark_channel(ink)
    address = ChannelAddress(
        channel_tentacle_id="lark",
        chat_type="dm",
        chat_id="u1",
        user_id="u1",
    )

    async def events() -> AsyncIterator[
        StreamEvents[ChannelOutput] | AgentRunResultEvent[ChannelOutput]
    ]:
        yield ResultSegmentEvent(
            segment=CardSegment(data=CardData(payload={"header": {"title": "t"}}))
        )

    await drive(channel, address, events())

    assert len(ink.created) == 1
    _, _, msg_type, content = ink.created[0]
    assert msg_type == "text"
    text = json.loads(content)["text"]
    assert "couldn't render this as a Lark card" in text
    assert '"header":{"title":"t"}' in text


async def test_lark_consume_renders_todo_checklist_card() -> None:
    ink = FakeLarkInk()
    channel = lark_channel(ink)
    address = ChannelAddress(
        channel_tentacle_id="lark",
        chat_type="dm",
        chat_id="u1",
        user_id="u1",
    )
    todo = Todo(conversation_id=uuid4(), ref="T1", content="Find the docs")

    async def events() -> AsyncIterator[
        StreamEvents[ChannelOutput] | AgentRunResultEvent[ChannelOutput]
    ]:
        yield TodoCreatedEvent(todo=todo)
        yield TodoCompletedEvent(todo=todo.model_copy(update={"status": "completed"}))

    await drive(channel, address, events())

    # First event posts the checklist card; later events patch it in place.
    todo_cards = [
        content for _, _, msg_type, content in ink.created if "Tasks" in content
    ]
    assert todo_cards
    assert "- [ ] Find the docs" in todo_cards[0]
    assert ink.patched
    assert "- [x] Find the docs" in ink.patched[-1][1]


async def test_lark_consume_renders_action_batch_cards() -> None:
    ink = FakeLarkInk()
    deferred = RecordingDeferredActions()
    channel = lark_channel(ink, deferred_actions=deferred)
    address = ChannelAddress(
        channel_tentacle_id="lark",
        chat_type="dm",
        chat_id="u1",
        user_id="u1",
    )
    # The lark question card serializes its batch id into the button state,
    # so the scripted actions need a real one.
    batch_id = uuid4()
    question, approval = batch_actions()
    question = question.model_copy(update={"batch_id": batch_id})
    approval = approval.model_copy(update={"batch_id": batch_id})

    await drive(
        channel,
        address,
        play(
            action_batch(
                batch_id=str(batch_id),
                questions=[question],
                approvals=[approval],
            )
        ),
    )

    # The question and approval each post as their own interactive card.
    assert [msg_type for _, _, msg_type, _ in ink.created] == [
        "interactive",
        "interactive",
    ]
    question_card = _loaded_json_object(ink.created[0][3])
    assert question_card["header"] == {
        "title": {"tag": "plain_text", "content": "Question"},
        "template": "blue",
    }
    assert "Which option should I take?" in ink.created[0][3]
    approval_card = _loaded_json_object(ink.created[1][3])
    assert approval_card["header"] == {
        "title": {"tag": "plain_text", "content": "Permission Required"},
        "template": "orange",
    }
    assert "deploy" in ink.created[1][3]
    # Both actions were marked presented with their card's message id.
    assert deferred.marked == [
        (question.id, "created-1"),
        (approval.id, "created-2"),
    ]


async def test_lark_timeline_opens_new_answer_card_after_mid_run_notice() -> None:
    ink = FakeLarkInk()
    channel = lark_channel(ink)
    address = ChannelAddress(
        channel_tentacle_id="lark",
        chat_type="dm",
        chat_id="u1",
        user_id="u1",
    )

    await drive(channel, address, play(mid_run_notice()))

    # The notice streamed into a first answer card which the rotation
    # finalized; the final answer opened a fresh card below the new activity.
    assert len(ink.stream_cards) == 2
    assert len(ink.finalized) == 2
    notice_card, answer_card = (card for card, _seq in ink.finalized)
    notice_text = "".join(
        content for card, content, _seq in ink.stream_updates if card is notice_card
    )
    answer_text = "".join(
        content for card, content, _seq in ink.stream_updates if card is answer_card
    )
    assert "trying another way" in notice_text
    assert "pinning the dependency" in answer_text
    # Both rounds' thinking and tool cards posted (and folded via patch).
    assert len(ink.created) == 4
    assert len(ink.patched) == 4


async def test_lark_answer_stream_renders_markdown_tables_verbatim() -> None:
    ink = FakeLarkInk()
    channel = lark_channel(ink)
    address = ChannelAddress(
        channel_tentacle_id="lark",
        chat_type="dm",
        chat_id="u1",
        user_id="u1",
    )
    table = "| Name | Value |\n| --- | --- |\n| A | 1 |"

    async def events() -> AsyncIterator[
        StreamEvents[ChannelOutput] | AgentRunResultEvent[ChannelOutput]
    ]:
        yield PartStartEvent(index=0, part=TextPart(content=table))
        yield AgentRunResultEvent(AgentRunResult(table))

    await drive(channel, address, events())

    # The table streams into the card as-is, for Lark's markdown to render.
    assert ink.stream_updates
    assert ink.stream_updates[-1][1] == table


async def test_lark_answer_updates_coalesce_off_the_drive_loop() -> None:
    # Answer-card updates run on a background flush task, so deltas that arrive
    # while a (slow) cardkit update is in flight coalesce into the next update
    # instead of blocking the drive loop (which would backpressure generation).
    class SlowFirstUpdateInk(FakeLarkInk):
        def __init__(self) -> None:
            super().__init__()
            self.update_started = asyncio.Event()
            self.release_update = asyncio.Event()
            self.update_calls = 0

        async def update_stream_card(
            self, card: object, *, content: str, sequence: int
        ) -> bool:
            self.update_calls += 1
            if self.update_calls == 1:
                self.update_started.set()
                await self.release_update.wait()
            return await super().update_stream_card(
                card,  # type: ignore[arg-type]
                content=content,
                sequence=sequence,
            )

    ink = SlowFirstUpdateInk()
    channel = lark_channel(ink)
    address = ChannelAddress(
        channel_tentacle_id="lark",
        chat_type="dm",
        chat_id="u1",
        user_id="u1",
    )

    async def events() -> AsyncIterator[
        StreamEvents[ChannelOutput] | AgentRunResultEvent[ChannelOutput]
    ]:
        yield PartStartEvent(index=0, part=TextPart(content="A"))
        # The first update ("A") is now in flight on the flush task.
        await ink.update_started.wait()
        yield PartDeltaEvent(index=0, delta=TextPartDelta(content_delta="B"))
        yield PartDeltaEvent(index=0, delta=TextPartDelta(content_delta="C"))
        ink.release_update.set()
        yield AgentRunResultEvent(AgentRunResult("ABC"))

    await drive(channel, address, events())

    contents = [content for _card, content, _seq in ink.stream_updates]
    # First update carried "A"; "B"/"C" that arrived while it was in flight
    # coalesced into a single follow-up update of the full "ABC" — the "AB"
    # intermediate state is skipped, never sent.
    assert contents[0] == "A"
    assert contents[-1] == "ABC"
    assert "AB" not in contents


async def test_lark_answer_card_opens_before_the_thinking_card_folds() -> None:
    # Creating and sending the answer card is two round trips, and folding the
    # thinking card is a third. Opening the answer card when the text part starts —
    # and signalling the flusher before folding — keeps all three off the stretch
    # between the last token and the first visible character.
    class OrderedInk(FakeLarkInk):
        def __init__(self) -> None:
            super().__init__()
            self.order: list[str] = []

        async def create_stream_card(
            self, card_data: str, *, element_id: str
        ) -> LarkStreamCard:
            self.order.append("open_answer_card")
            return await super().create_stream_card(card_data, element_id=element_id)

        async def patch_card(self, message_id: str, content: str) -> bool:
            if "Thought for" in content:
                self.order.append("fold_thinking")
            return await super().patch_card(message_id, content)

    ink = OrderedInk()
    channel = lark_channel(ink)
    address = ChannelAddress(
        channel_tentacle_id="lark",
        chat_type="dm",
        chat_id="u1",
        user_id="u1",
    )

    async def events() -> AsyncIterator[
        StreamEvents[ChannelOutput] | AgentRunResultEvent[ChannelOutput]
    ]:
        yield PartStartEvent(index=0, part=ThinkingPart(content="checking"))
        yield PartStartEvent(index=1, part=TextPart(content="answer"))
        yield AgentRunResultEvent(AgentRunResult("answer"))

    await drive(channel, address, events())

    assert ink.order == ["open_answer_card", "fold_thinking"]


async def test_lark_subagents_own_cards_separate_from_parent_and_siblings() -> None:
    ink = FakeLarkInk()
    channel = lark_channel(ink)
    address = ChannelAddress(
        channel_tentacle_id="lark",
        chat_type="dm",
        chat_id="u1",
        user_id="u1",
    )

    first_activity = SubagentActivity("call-a", "commission", "audit")
    second_activity = SubagentActivity("call-b", "commission", "tests")
    async with channel.feelers.timeline.open(address) as parent:
        await parent.thinking_start()
        await parent.thinking_delta("parent work")
        async with (
            parent.open_subagent(first_activity) as first,
            parent.open_subagent(second_activity) as second,
        ):
            await first.append_response("audit result")
            await second.append_response("test result")
            await first.settle("completed")
            await second.settle("failed", "one failure")
    parent_card_id = "created-1"

    assert len(ink.created) == 3
    patched_ids = [message_id for message_id, _content in ink.patched]
    assert {"created-2", "created-3"} <= set(patched_ids)
    first_final = next(
        content
        for message_id, content in reversed(ink.patched)
        if message_id == "created-2"
    )
    second_final = next(
        content
        for message_id, content in reversed(ink.patched)
        if message_id == "created-3"
    )
    assert '"expanded":false' in first_final
    assert "audit result" in first_final
    assert "test result" not in first_final
    assert '"expanded":false' in second_final
    assert "test result" in second_final
    assert "audit result" not in second_final
    assert patched_ids.count(parent_card_id) >= 1
