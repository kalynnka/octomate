"""Unit tests for ConversationManager against an in-memory SQLite."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import datetime, timezone

import pytest
from pydantic_ai.messages import (
    ModelRequest as RawModelRequest,
)
from pydantic_ai.messages import (
    ModelResponse as RawModelResponse,
)
from pydantic_ai.messages import TextPart, ToolCallPart, UserPromptPart
from sqlalchemy.ext.asyncio import AsyncEngine

import uuid

from uuid_utils.compat import uuid7

from octomate.managers import ConversationManager


@pytest.fixture(autouse=True)
async def _db(in_memory_engine: AsyncEngine) -> AsyncIterator[None]:
    yield


_THREAD = uuid7()


def _thread() -> uuid.UUID:
    return _THREAD


async def test_ensure_is_idempotent() -> None:
    service = ConversationManager()
    a = await service.ensure(_thread(), agent_tentacle_id="inkling")
    b = await service.ensure(_thread(), agent_tentacle_id="inkling")
    assert a.id == b.id


async def test_permission_defaults_and_grant_round_trip() -> None:
    service = ConversationManager()
    convo = await service.ensure(_thread(), agent_tentacle_id="claude")
    assert convo.permission_mode == "default"
    assert convo.allowed_tools == []

    await service.grant_session_tool(convo, "Bash")
    await service.grant_session_tool(convo, "Bash")  # idempotent
    await service.grant_session_tool(convo, "Write")

    # A fresh manager reads the persisted grants back from the database.
    fresh = ConversationManager()
    reloaded = await fresh.ensure(_thread(), agent_tentacle_id="claude")
    assert reloaded.allowed_tools == ["Bash", "Write"]


async def test_ensure_loads_existing_conversation() -> None:
    first = ConversationManager()
    created = await first.ensure(_thread(), agent_tentacle_id="inkling")

    fresh = ConversationManager()
    fetched = await fresh.ensure(_thread(), agent_tentacle_id="inkling")
    assert fetched.id == created.id


async def test_ensure_is_per_agent() -> None:
    service = ConversationManager()
    inkling = await service.ensure(_thread(), agent_tentacle_id="inkling")
    assert inkling.agent_tentacle_id == "inkling"

    # A different agent at the same location gets its own conversation; the
    # original agent's conversation is untouched, and re-ensuring the original
    # agent returns it.
    other = await ConversationManager().ensure(_thread(), agent_tentacle_id="other")
    assert other.id != inkling.id
    assert other.agent_tentacle_id == "other"

    again = await ConversationManager().ensure(_thread(), agent_tentacle_id="inkling")
    assert again.id == inkling.id


async def test_record_run_creates_run_and_persists_messages() -> None:
    service = ConversationManager()
    conversation = await service.ensure(_thread(), agent_tentacle_id="inkling")

    run_id = "run-1"
    raw = [
        RawModelRequest(
            parts=[UserPromptPart(content="hi")],
            run_id=run_id,
            timestamp=datetime.now(timezone.utc),
        ),
        RawModelResponse(
            parts=[TextPart(content="hello")],
            run_id=run_id,
            timestamp=datetime.now(timezone.utc),
            finish_reason="stop",
        ),
    ]
    await service.record_agent_run(conversation, run_id=run_id, messages=raw)

    fresh = ConversationManager()
    reloaded = await fresh.ensure(_thread(), agent_tentacle_id="inkling")
    runs = list(reloaded.runs)
    assert len(runs) == 1
    assert runs[0].id == run_id
    assert runs[0].name is None
    listed = list(runs[0].messages)
    assert len(listed) == 2
    kinds = {type(m).__name__ for m in listed}
    assert kinds == {"ModelRequest", "ModelResponse"}
    assert len(list(reloaded.messages)) == 2


async def test_record_run_preserves_finish_reason() -> None:
    """The blessed ModelResponse round-trips pydantic-ai's finish_reason."""
    service = ConversationManager()
    conversation = await service.ensure(_thread(), agent_tentacle_id="inkling")
    run_id = "run-fr"

    await service.record_agent_run(
        conversation,
        run_id=run_id,
        messages=[
            RawModelResponse(
                parts=[TextPart(content="halt")],
                run_id=run_id,
                timestamp=datetime.now(timezone.utc),
                finish_reason="tool_call",
            ),
        ],
    )

    fresh = ConversationManager()
    reloaded = await fresh.ensure(_thread(), agent_tentacle_id="inkling")
    msgs = list(reloaded.messages)
    assert len(msgs) == 1
    assert msgs[0].finish_reason == "tool_call"


async def test_record_run_persists_name() -> None:
    service = ConversationManager()
    conversation = await service.ensure(_thread(), agent_tentacle_id="inkling")

    await service.record_agent_run(
        conversation,
        run_id="run-named",
        name="triage",
        messages=[
            RawModelResponse(
                parts=[TextPart(content="route")],
                run_id="run-named",
                timestamp=datetime.now(timezone.utc),
                finish_reason="stop",
            ),
        ],
    )

    fresh = ConversationManager()
    reloaded = await fresh.ensure(_thread(), agent_tentacle_id="inkling")
    runs = list(reloaded.runs)
    assert len(runs) == 1
    assert runs[0].name == "triage"


async def test_record_run_no_op_for_empty_list() -> None:
    service = ConversationManager()
    conversation = await service.ensure(_thread(), agent_tentacle_id="inkling")
    await service.record_agent_run(conversation, run_id="empty", messages=[])
    fresh = ConversationManager()
    reloaded = await fresh.ensure(_thread(), agent_tentacle_id="inkling")
    assert list(reloaded.runs) == []


async def test_record_run_syncs_cached_history() -> None:
    service = ConversationManager()
    conversation = await service.ensure(_thread(), agent_tentacle_id="inkling")
    assert list(conversation.messages) == []

    await service.record_agent_run(
        conversation,
        run_id="run-1",
        messages=[
            RawModelRequest(
                parts=[UserPromptPart(content="hi")],
                run_id="run-1",
                timestamp=datetime.now(timezone.utc),
            ),
            RawModelResponse(
                parts=[TextPart(content="hello")],
                run_id="run-1",
                timestamp=datetime.now(timezone.utc),
                finish_reason="stop",
            ),
        ],
    )

    # record_agent_run keeps the cached conversation coherent; a hot ensure()
    # (cache hit, no cold reload) reflects the new run.
    hot = await service.ensure(_thread(), agent_tentacle_id="inkling")
    assert len(list(hot.messages)) == 2


async def test_drop_trailing_deferral_removes_from_cache_and_db() -> None:
    service = ConversationManager()
    conversation = await service.ensure(_thread(), agent_tentacle_id="inkling")
    await service.record_agent_run(
        conversation,
        run_id="run-defer",
        messages=[
            RawModelRequest(
                parts=[UserPromptPart(content="do it")],
                run_id="run-defer",
                timestamp=datetime.now(timezone.utc),
            ),
            RawModelResponse(
                parts=[
                    ToolCallPart(
                        tool_name="ask_questions",
                        args={"questions": [{"question": "?"}]},
                        tool_call_id="call_1",
                    )
                ],
                run_id="run-defer",
                timestamp=datetime.now(timezone.utc),
            ),
        ],
    )

    # ensure() returns the conversation synced by record_agent_run.
    conversation = await service.ensure(_thread(), agent_tentacle_id="inkling")
    dropped = await service.drop_trailing_deferral(conversation)
    assert dropped is not None

    # The deferral is gone from the cache (hot) and the DB (cold reload).
    hot = await service.ensure(_thread(), agent_tentacle_id="inkling")
    assert [type(m).__name__ for m in hot.messages] == ["ModelRequest"]
    cold = await ConversationManager().ensure(_thread(), agent_tentacle_id="inkling")
    assert [type(m).__name__ for m in cold.messages] == ["ModelRequest"]

    # The trailing message is now a request, not a deferral — nothing to drop.
    conversation = await service.ensure(_thread(), agent_tentacle_id="inkling")
    assert await service.drop_trailing_deferral(conversation) is None
