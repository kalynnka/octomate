"""Ordering follows archive identity or context position, without hydrating history."""

from datetime import UTC, datetime, timedelta

import pytest
from pydantic_ai.messages import TextPart, UserPromptPart
from sqlalchemy.ext.asyncio import AsyncEngine
from uuid_utils.compat import uuid7

from octomate.managers.conversation import ConversationManager
from octomate.schemas.compactions import (
    Compaction,
    CompactionMembership,
    ExternalCompaction,
)
from octomate.schemas.conversation import ConversationCompaction
from octomate.schemas.messages import ModelRequest, ModelResponse
from octomate.tentacles.deepseek.compactions import SurfaceCompaction, SurfaceMessage
from octomate.types.compactions import CompactionStage
from tests.support.managers import a_thread


def test_compaction_order_uses_recorded_time_then_uuid_across_variants() -> None:
    conversation_id = uuid7()
    now = datetime(2026, 11, 13, tzinfo=UTC)
    late = Compaction(
        conversation_id=conversation_id,
        recorded_at=now + timedelta(seconds=1),
        happened_at=now - timedelta(days=1),
        membership_complete=True,
    )
    first = ExternalCompaction(conversation_id=conversation_id, recorded_at=now)
    second = Compaction(conversation_id=conversation_id, recorded_at=now)
    records = [late, second, first]
    assert sorted(records) == [first, second, late]
    assert sorted(records, reverse=True) == [late, second, first]
    assert first < second < late
    assert late > second > first


def test_conversation_links_keep_unknown_positions_first_and_ties_stable() -> None:
    conversation_id = uuid7()
    links = [
        ConversationCompaction(
            conversation_id=conversation_id, compaction_id=uuid7(), position=position
        )
        for position in (1, None, 0, None, 1)
    ]
    assert sorted(links) == [links[1], links[3], links[2], links[0], links[4]]
    assert links[1] < links[2] < links[0]
    assert links[0] > links[2] > links[1]
    assert not links[1] < links[3]
    assert not links[1] > links[3]


@pytest.mark.parametrize("stage", ["before", "after"])
def test_membership_order_uses_position_not_message_uuid(
    stage: CompactionStage,
) -> None:
    compaction_id = uuid7()
    members = [
        CompactionMembership(
            compaction_id=compaction_id,
            stage=stage,
            position=position,
            model_message_id=uuid7(),
        )
        for position in (2, 0, 1)
    ]
    assert sorted(members) == [members[1], members[2], members[0]]
    assert members[1] < members[2] < members[0]
    assert members[0] > members[2] > members[1]


async def test_mixed_persisted_messages_sort_by_uuid_not_optional_timestamps(
    in_memory_engine: AsyncEngine,
) -> None:
    manager = ConversationManager()
    conversation = await manager.ensure(await a_thread(), agent_tentacle_id="agent")
    now = datetime(2026, 11, 13, tzinfo=UTC)
    first = ModelRequest(parts=[UserPromptPart("First")], timestamp=now)
    second = ModelResponse(
        parts=[TextPart("Second")], timestamp=now - timedelta(days=1)
    )
    third = ModelRequest(parts=[UserPromptPart("Third")], timestamp=None)
    await manager.record_agent_run(conversation, str(uuid7()), [first, second, third])
    messages = await manager.history(conversation.id)
    scrambled = [messages[2], messages[0], messages[1]]
    assert [message.id for message in sorted(scrambled)] == [
        first.id,
        second.id,
        third.id,
    ]
    assert messages[0] < messages[1] < messages[2]
    assert messages[2] > messages[1] > messages[0]


def test_dsh_boundaries_use_source_sequence_instead_of_record_order() -> None:
    conversation_id = uuid7()
    first_record = Compaction(conversation_id=conversation_id)
    second_record = Compaction(conversation_id=conversation_id)
    checkpoint = SurfaceMessage(uuid7(), [20])
    later = SurfaceCompaction(first_record, [1], 10, checkpoint)
    earlier = SurfaceCompaction(second_record, [1], 5, checkpoint)
    tied = SurfaceCompaction(first_record, [1, 2], 5, checkpoint)
    assert sorted([later, earlier, tied]) == [earlier, tied, later]
    assert earlier < later
    assert later > earlier
    assert earlier <= tied
    assert tied >= earlier
