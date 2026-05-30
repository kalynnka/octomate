from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
from arcanus import RelationCollection
from pydantic import ValidationError
from pydantic_ai.messages import ToolCallPart
from pydantic_ai.tools import DeferredToolRequests
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    async_sessionmaker,
    create_async_engine,
)
from uuid_utils.compat import uuid7

import octomate.database as database
from octomate.managers.conversations import ConversationManager
from octomate.managers.deferred import DeferredActionManager
from octomate.models import Base
from octomate.schemas.base import sqlalchemy_materia
from octomate.schemas.conversation import ConversationKey
from octomate.schemas.deferred import (
    DeferredActionBatch,
    DeferredActionCollection,
    DeferredApproval,
    DeferredQuestion,
)
from octomate.schemas.triage import TriageDecision


@pytest.fixture
async def in_memory_engine(
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncIterator[AsyncEngine]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    maker = async_sessionmaker(
        engine,
        class_=database.AsyncSession,
        expire_on_commit=False,
    )

    database.engine.cache_clear()
    database.session_maker.cache_clear()
    monkeypatch.setattr(database, "engine", lambda: engine)
    monkeypatch.setattr(database, "session_maker", lambda: maker)

    with sqlalchemy_materia:
        yield engine

    await engine.dispose()


def _key() -> ConversationKey:
    return ConversationKey(
        channel_tentacle_id="slack",
        chat_type="private",
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


def test_deferred_action_batch_accepts_validated_actions() -> None:
    requests = _requests()
    actions = DeferredActionCollection.validate_python(requests)
    questions = [
        action for action in actions if isinstance(action, DeferredQuestion)
    ]
    approvals = [
        action for action in actions if isinstance(action, DeferredApproval)
    ]
    key = _key()

    batch = DeferredActionBatch(
        conversation_id=uuid7(),
        agent_tentacle_id="inkling",
        run_name="reception",
        source_key=key,
        target_key=key,
        target_mode="main",
        decision=TriageDecision(action="reception", reason="needs input"),
        requests=requests,
        questions=RelationCollection(questions),
        approvals=RelationCollection(approvals),
    )

    assert [action.kind for action in batch.questions] == ["question"]
    assert [action.kind for action in batch.approvals] == ["approval"]


async def test_deferred_action_batch_relationships_filter_by_kind(
    in_memory_engine: AsyncEngine,
) -> None:
    key = _key()
    conversation = await ConversationManager().ensure(key, agent_tentacle_id="inkling")
    created = await DeferredActionManager().create_batch(
        conversation=conversation,
        agent_tentacle_id="inkling",
        run_name="reception",
        source_key=key,
        target_key=key,
        target_mode="main",
        decision=TriageDecision(action="reception", reason="needs input"),
        requests=_requests(),
    )

    reloaded = await DeferredActionManager().get_batch_context(created.batch.id)

    assert [action.kind for action in reloaded.questions] == ["question"]
    assert [action.kind for action in reloaded.approvals] == ["approval"]


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
