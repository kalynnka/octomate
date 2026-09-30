from __future__ import annotations

from uuid import UUID, uuid4

import pytest
from arcanus import RelationCollection
from pydantic import ValidationError
from pydantic_ai.messages import ToolCallPart
from pydantic_ai.tools import DeferredToolRequests
from sqlalchemy.ext.asyncio import AsyncEngine
from uuid_utils.compat import uuid7

from octomate.database import async_session
from octomate.managers.conversation import ConversationManager
from octomate.managers.deferred import DeferredActionManager
from octomate.schemas.awakes import DeferredActionBatchResponse
from octomate.schemas.conversation import ChannelAddress
from octomate.schemas.deferred import (
    DeferredActionBatch,
    DeferredActionCollection,
    DeferredApproval,
    DeferredQuestion,
)
from octomate.schemas.triage import SummonDecision
from octomate.types.deferred import DeferredResponseMode
from tests.support.managers import a_thread


def _key() -> ChannelAddress:
    return ChannelAddress(
        channel_tentacle_id="slack",
        chat_type="dm",
        chat_id="alice",
        user_id="alice",
    )


def _requests() -> DeferredToolRequests:
    return DeferredToolRequests(
        calls=[
            ToolCallPart(
                tool_name="ask_questions",
                args={"questions": [{"question": "Deploy window?"}]},
                tool_call_id="call_question",
            )
        ],
        approvals=[
            ToolCallPart(
                tool_name="shell",
                args={"cmd": "git status"},
                tool_call_id="call_approval",
            )
        ],
    )


async def _create_batch(
    response_mode: DeferredResponseMode = "resume",
    *,
    batch_id: UUID | None = None,
) -> DeferredActionBatch:
    address = _key()
    conversation = await ConversationManager().ensure(
        await a_thread(), agent_tentacle_id="inkling"
    )
    return await DeferredActionManager().create_batch(
        batch_id=batch_id,
        response_mode=response_mode,
        conversation=conversation,
        agent_tentacle_id="inkling",
        run_name="react",
        source_address=address,
        target_address=address,
        target_mode="main",
        decision=SummonDecision(
            action="summon",
            agent_id="inkling",
            model="test",
            reason="needs input",
            hint="needs input",
            summon="needs input",
        )
        if response_mode == "resume"
        else None,
        requests=_requests(),
    )


@pytest.mark.parametrize("batch_id", [None, uuid7()], ids=["generated", "supplied"])
async def test_create_batch_uses_uuid7(
    in_memory_engine: AsyncEngine,
    batch_id: UUID | None,
) -> None:
    created = await _create_batch(batch_id=batch_id)

    assert created.id.version == 7
    if batch_id is not None:
        assert created.id == batch_id
    reloaded = await DeferredActionManager().get_batch(created.id)
    assert reloaded.id == created.id


@pytest.mark.parametrize(
    "batch_id",
    [UUID(int=0), UUID(int=0, version=1), uuid4()],
    ids=["nil", "uuid1", "uuid4"],
)
async def test_create_batch_rejects_non_uuid7_before_persistence(
    in_memory_engine: AsyncEngine,
    batch_id: UUID,
) -> None:
    with pytest.raises(ValidationError) as error:
        await _create_batch(batch_id=batch_id)

    assert error.value.errors()[0]["type"] == "uuid_version"
    async with async_session() as session:
        assert not await session.list(DeferredActionBatch)


def test_deferred_reply_rejects_non_uuid7_batch_id() -> None:
    with pytest.raises(ValidationError) as error:
        DeferredActionBatchResponse.model_validate({"batch_id": str(uuid4())})

    assert error.value.errors()[0]["type"] == "uuid_version"


def test_deferred_action_rejects_non_uuid7_id() -> None:
    with pytest.raises(ValidationError) as error:
        DeferredQuestion(
            id=uuid4(),
            tool_name="ask_questions",
            tool_call_id="call_question",
            args={"question": "Deploy window?"},
        )

    assert error.value.errors()[0]["loc"] == ("id",)
    assert error.value.errors()[0]["type"] == "uuid_version"


def test_deferred_batch_rejects_non_uuid7_conversation_id() -> None:
    address = _key()
    with pytest.raises(ValidationError) as error:
        DeferredActionBatch(
            response_mode="live",
            conversation_id=uuid4(),
            agent_tentacle_id="deepseek",
            source_address=address,
            target_address=address,
            requests=DeferredToolRequests(),
        )

    assert error.value.errors()[0]["loc"] == ("conversation_id",)
    assert error.value.errors()[0]["type"] == "uuid_version"


def test_deferred_action_batch_accepts_validated_actions() -> None:
    requests = _requests()
    actions = DeferredActionCollection.validate_python(requests)
    questions = [action for action in actions if isinstance(action, DeferredQuestion)]
    approvals = [action for action in actions if isinstance(action, DeferredApproval)]
    address = _key()

    batch = DeferredActionBatch(
        response_mode="resume",
        conversation_id=uuid7(),
        agent_tentacle_id="inkling",
        run_name="react",
        source_address=address,
        target_address=address,
        target_mode="main",
        decision=SummonDecision(
            action="summon",
            agent_id="inkling",
            model="test",
            reason="needs input",
            hint="needs input",
            summon="needs input",
        ),
        requests=requests,
        questions=RelationCollection(questions),
        approvals=RelationCollection(approvals),
    )

    assert [action.kind for action in batch.questions] == ["question"]
    assert [action.kind for action in batch.approvals] == ["approval"]


@pytest.mark.parametrize("response_mode", ["live", "resume"])
async def test_deferred_action_batch_relationships_filter_by_kind(
    in_memory_engine: AsyncEngine,
    response_mode: DeferredResponseMode,
) -> None:
    created = await _create_batch(response_mode)

    assert [action.batch_id for action in created.questions] == [created.id]
    assert [action.batch_id for action in created.approvals] == [created.id]

    reloaded = await DeferredActionManager().get_batch(created.id)
    assert reloaded.response_mode == response_mode

    assert [action.kind for action in reloaded.questions] == ["question"]
    assert [action.kind for action in reloaded.approvals] == ["approval"]
    assert [action.batch_id for action in reloaded.questions] == [created.id]
    assert [action.batch_id for action in reloaded.approvals] == [created.id]


def test_deferred_questions_sort_by_position() -> None:
    first = DeferredQuestion(
        tool_name="ask_questions",
        tool_call_id="call_question",
        position=1,
        args={"question": "First?"},
    )
    second = DeferredQuestion(
        tool_name="ask_questions",
        tool_call_id="call_question",
        position=2,
        args={"question": "Second?"},
    )

    assert sorted([second, first]) == [first, second]
    assert first < second
    assert second > first


def test_deferred_question_choices_are_limited_to_three() -> None:
    with pytest.raises(ValidationError):
        DeferredQuestion(
            tool_name="ask_questions",
            tool_call_id="call_question",
            args={
                "question": "Ocean zone?",
                "choices": [
                    "Coral Reef",
                    "Kelp Forest",
                    "Open Ocean",
                    "Deep Sea Trench",
                ],
            },
        )


async def test_resolve_batch_applies_answers_and_approvals(
    in_memory_engine: AsyncEngine,
) -> None:
    created = await _create_batch()
    question = next(iter(created.questions))
    approval = next(iter(created.approvals))

    resolved = await DeferredActionManager().resolve_batch(
        DeferredActionBatchResponse(
            batch_id=created.id,
            answers={question.id: "tonight"},
            approvals={approval.id: True},
            responder_id="alice",
        )
    )

    assert [action.status for action in resolved.questions] == ["answered"]
    assert [action.status for action in resolved.approvals] == ["approved"]
    assert [action.responder_id for action in resolved.questions] == ["alice"]
    assert resolved.status == "resolved"

    results = resolved.build_results()
    assert results.calls["call_question"] == ["tonight"]
    assert results.approvals["call_approval"] is True


async def test_mark_batch_sets_status_and_completed_at(
    in_memory_engine: AsyncEngine,
) -> None:
    created = await _create_batch()
    assert created.completed_at is None

    await DeferredActionManager().mark_batch(created.id, "completed", completed=True)

    reloaded = await DeferredActionManager().get_batch(created.id)
    assert reloaded.status == "completed"
    assert reloaded.completed_at is not None


async def test_mark_action_presented_noops_for_unknown_action(
    in_memory_engine: AsyncEngine,
) -> None:
    await DeferredActionManager().mark_action_presented(uuid4(), "msg-1")
