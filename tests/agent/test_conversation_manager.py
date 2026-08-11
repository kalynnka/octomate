"""Unit tests for ConversationManager against an in-memory SQLite."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
import sqlalchemy.exc
from pydantic import ValidationError
from pydantic_ai.messages import (
    ModelRequest as RawModelRequest,
)
from pydantic_ai.messages import (
    ModelResponse as RawModelResponse,
)
from pydantic_ai.messages import TextPart, ToolCallPart, UserPromptPart
from sqlalchemy.ext.asyncio import AsyncEngine
from uuid_utils.compat import uuid7

from octomate.database import async_session
from octomate.managers import ConversationManager
from octomate.schemas.conversation import Conversation
from octomate.schemas.runs import AgentRun, ExternalAgentRun
from tests.support.managers import a_thread


@pytest.fixture(autouse=True)
async def _db(in_memory_engine: AsyncEngine) -> None:
    return


async def _thread() -> uuid.UUID:
    return await a_thread()


async def test_ensure_is_idempotent() -> None:
    service = ConversationManager()
    a = await service.ensure(await _thread(), agent_tentacle_id="inkling")
    b = await service.ensure(await _thread(), agent_tentacle_id="inkling")
    assert a.id == b.id


async def test_subagents_lists_only_the_parents_own_hands() -> None:
    service = ConversationManager()
    thread = await _thread()
    parent = await service.ensure(thread, agent_tentacle_id="inkling")
    other = await service.ensure(thread, agent_tentacle_id="claude")
    audit = await service.ensure(
        thread,
        agent_tentacle_id="claude",
        subagent_id="repo-audit",
        parent_conversation_id=parent.id,
    )
    docs = await service.ensure(
        thread,
        agent_tentacle_id="codex",
        subagent_id="docs",
        parent_conversation_id=parent.id,
    )
    await service.ensure(
        thread,
        agent_tentacle_id="codex",
        subagent_id="stray",
        parent_conversation_id=other.id,
    )

    hands = await service.subagents(parent.id)

    assert {hand.id for hand in hands} == {audit.id, docs.id}
    assert {hand.subagent_id for hand in hands} == {"repo-audit", "docs"}


async def test_permission_defaults_and_grant_round_trip() -> None:
    service = ConversationManager()
    convo = await service.ensure(await _thread(), agent_tentacle_id="claude")
    # No project, so nothing is declared and the agent's configured default decides.
    assert convo.permission_mode is None
    assert convo.allowed_tools == []

    await service.grant_session_tool(convo, "Bash")
    await service.grant_session_tool(convo, "Bash")  # idempotent
    await service.grant_session_tool(convo, "Write")

    # A fresh manager reads the persisted grants back from the database.
    fresh = ConversationManager()
    reloaded = await fresh.ensure(await _thread(), agent_tentacle_id="claude")
    assert reloaded.allowed_tools == ["Bash", "Write"]


async def test_ensure_loads_existing_conversation() -> None:
    first = ConversationManager()
    created = await first.ensure(await _thread(), agent_tentacle_id="inkling")

    fresh = ConversationManager()
    fetched = await fresh.ensure(await _thread(), agent_tentacle_id="inkling")
    assert fetched.id == created.id


async def test_ensure_is_per_agent() -> None:
    service = ConversationManager()
    inkling = await service.ensure(await _thread(), agent_tentacle_id="inkling")
    assert inkling.agent_tentacle_id == "inkling"

    # A different agent at the same location gets its own conversation; the
    # original agent's conversation is untouched, and re-ensuring the original
    # agent returns it.
    other = await ConversationManager().ensure(
        await _thread(), agent_tentacle_id="other"
    )
    assert other.id != inkling.id
    assert other.agent_tentacle_id == "other"

    again = await ConversationManager().ensure(
        await _thread(), agent_tentacle_id="inkling"
    )
    assert again.id == inkling.id


async def test_record_run_creates_run_and_persists_messages() -> None:
    service = ConversationManager()
    conversation = await service.ensure(await _thread(), agent_tentacle_id="inkling")

    run_id = "run-1"
    raw = [
        RawModelRequest(
            parts=[UserPromptPart(content="hi")],
            run_id=run_id,
            timestamp=datetime.now(UTC),
        ),
        RawModelResponse(
            parts=[TextPart(content="hello")],
            run_id=run_id,
            timestamp=datetime.now(UTC),
            finish_reason="stop",
        ),
    ]
    await service.record_agent_run(conversation, run_id=run_id, messages=raw)

    fresh = ConversationManager()
    reloaded = await fresh.ensure(await _thread(), agent_tentacle_id="inkling")
    runs = list(reloaded.runs)
    assert len(runs) == 1
    assert runs[0].id == run_id
    assert runs[0].name is None
    listed = list(runs[0].messages)
    assert len(listed) == 2
    kinds = {type(m).__name__ for m in listed}
    assert kinds == {"ModelRequest", "ModelResponse"}
    assert len(list(reloaded.messages)) == 2


async def test_external_run_reads_back_as_its_variant() -> None:
    """record_external_run persists the `external` variant with its transcript
    coordinates; record_agent_run stays `octomate`. A fresh manager reads each back as
    its type from the one polymorphic table."""
    service = ConversationManager()
    conversation = await service.ensure(
        await _thread(), agent_tentacle_id="claude-native"
    )

    def _msgs(run_id: str) -> list[RawModelRequest | RawModelResponse]:
        return [
            RawModelRequest(
                parts=[UserPromptPart(content="hi")],
                run_id=run_id,
                timestamp=datetime.now(UTC),
            ),
            RawModelResponse(
                parts=[TextPart(content="hello")],
                run_id=run_id,
                timestamp=datetime.now(UTC),
                finish_reason="stop",
            ),
        ]

    await service.record_agent_run(
        conversation, run_id="oct", messages=_msgs("oct"), name="inkling"
    )
    await service.record_external_run(
        conversation,
        run_id="ext",
        messages=_msgs("ext"),
        name="claude-native",
        external_session_id="sess-9",
        source="claude-vscode",
        start_offset=0,
        end_offset=42,
        last_line_uuid="u-last",
    )

    fresh = ConversationManager()
    reloaded = await fresh.ensure(await _thread(), agent_tentacle_id="claude-native")
    by_id = {run.id: run for run in reloaded.runs}

    octomate_run = by_id["oct"]
    assert isinstance(octomate_run, AgentRun)
    assert not isinstance(octomate_run, ExternalAgentRun)

    external_run = by_id["ext"]
    assert isinstance(external_run, ExternalAgentRun)
    assert external_run.external_session_id == "sess-9"
    assert external_run.source == "claude-vscode"
    assert (external_run.start_offset, external_run.end_offset) == (0, 42)
    assert external_run.last_line_uuid == "u-last"
    # session_id doubles as the conversation's resumable handle.
    assert reloaded.external_id == "sess-9"


async def test_record_run_preserves_finish_reason() -> None:
    """The blessed ModelResponse round-trips pydantic-ai's finish_reason."""
    service = ConversationManager()
    conversation = await service.ensure(await _thread(), agent_tentacle_id="inkling")
    run_id = "run-fr"

    await service.record_agent_run(
        conversation,
        run_id=run_id,
        messages=[
            RawModelResponse(
                parts=[TextPart(content="halt")],
                run_id=run_id,
                timestamp=datetime.now(UTC),
                finish_reason="tool_call",
            ),
        ],
    )

    fresh = ConversationManager()
    reloaded = await fresh.ensure(await _thread(), agent_tentacle_id="inkling")
    msgs = list(reloaded.messages)
    assert len(msgs) == 1
    assert msgs[0].finish_reason == "tool_call"


async def test_record_run_persists_name() -> None:
    service = ConversationManager()
    conversation = await service.ensure(await _thread(), agent_tentacle_id="inkling")

    await service.record_agent_run(
        conversation,
        run_id="run-named",
        name="triage",
        messages=[
            RawModelResponse(
                parts=[TextPart(content="route")],
                run_id="run-named",
                timestamp=datetime.now(UTC),
                finish_reason="stop",
            ),
        ],
    )

    fresh = ConversationManager()
    reloaded = await fresh.ensure(await _thread(), agent_tentacle_id="inkling")
    runs = list(reloaded.runs)
    assert len(runs) == 1
    assert runs[0].name == "triage"


async def test_record_run_no_op_for_empty_list() -> None:
    service = ConversationManager()
    conversation = await service.ensure(await _thread(), agent_tentacle_id="inkling")
    await service.record_agent_run(conversation, run_id="empty", messages=[])
    fresh = ConversationManager()
    reloaded = await fresh.ensure(await _thread(), agent_tentacle_id="inkling")
    assert list(reloaded.runs) == []


async def test_record_run_syncs_cached_history() -> None:
    service = ConversationManager()
    conversation = await service.ensure(await _thread(), agent_tentacle_id="inkling")
    assert list(conversation.messages) == []

    await service.record_agent_run(
        conversation,
        run_id="run-1",
        messages=[
            RawModelRequest(
                parts=[UserPromptPart(content="hi")],
                run_id="run-1",
                timestamp=datetime.now(UTC),
            ),
            RawModelResponse(
                parts=[TextPart(content="hello")],
                run_id="run-1",
                timestamp=datetime.now(UTC),
                finish_reason="stop",
            ),
        ],
    )

    # record_agent_run keeps the cached conversation coherent; a hot ensure()
    # (cache hit, no cold reload) reflects the new run.
    hot = await service.ensure(await _thread(), agent_tentacle_id="inkling")
    assert len(list(hot.messages)) == 2


async def test_record_second_run_keeps_prior_run_messages() -> None:
    # Recording a second run through the same live conversation reference persists
    # only the new run rather than re-merging the whole conversation graph (which
    # could null a NOT NULL run_id). Both runs and all their messages survive, and
    # the passed reference is never mutated.
    service = ConversationManager()
    first = await service.ensure(await _thread(), agent_tentacle_id="inkling")
    await service.record_agent_run(
        first,
        run_id="run-1",
        messages=[
            RawModelRequest(
                parts=[UserPromptPart(content="first")],
                run_id="run-1",
                timestamp=datetime.now(UTC),
            ),
            RawModelResponse(
                parts=[TextPart(content="one")],
                run_id="run-1",
                timestamp=datetime.now(UTC),
                finish_reason="stop",
            ),
        ],
    )

    # The next turn records again through the SAME cached conversation reference
    # (as a live tentacle holds it), whose run collection is stale vs the DB.
    await service.record_agent_run(
        first,
        run_id="run-2",
        messages=[
            RawModelRequest(
                parts=[UserPromptPart(content="second")],
                run_id="run-2",
                timestamp=datetime.now(UTC),
            ),
            RawModelResponse(
                parts=[TextPart(content="two")],
                run_id="run-2",
                timestamp=datetime.now(UTC),
                finish_reason="stop",
            ),
        ],
    )

    # The passed conversation reference is never mutated (no run appended to it),
    # so its collections can't go stale and drive a graph re-merge.
    assert list(first.runs) == []

    fresh = ConversationManager()
    reloaded = await fresh.ensure(await _thread(), agent_tentacle_id="inkling")
    assert {run.id for run in reloaded.runs} == {"run-1", "run-2"}
    assert len(list(reloaded.messages)) == 4


async def test_drop_trailing_deferral_removes_from_cache_and_db() -> None:
    service = ConversationManager()
    conversation = await service.ensure(await _thread(), agent_tentacle_id="inkling")
    await service.record_agent_run(
        conversation,
        run_id="run-defer",
        messages=[
            RawModelRequest(
                parts=[UserPromptPart(content="do it")],
                run_id="run-defer",
                timestamp=datetime.now(UTC),
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
                timestamp=datetime.now(UTC),
            ),
        ],
    )

    # ensure() returns the conversation synced by record_agent_run.
    conversation = await service.ensure(await _thread(), agent_tentacle_id="inkling")
    dropped = await service.drop_trailing_deferral(conversation)
    assert dropped is not None

    # The deferral is gone from the cache (hot) and the DB (cold reload).
    hot = await service.ensure(await _thread(), agent_tentacle_id="inkling")
    assert [type(m).__name__ for m in hot.messages] == ["ModelRequest"]
    cold = await ConversationManager().ensure(
        await _thread(), agent_tentacle_id="inkling"
    )
    assert [type(m).__name__ for m in cold.messages] == ["ModelRequest"]

    # The trailing message is now a request, not a deferral — nothing to drop.
    conversation = await service.ensure(await _thread(), agent_tentacle_id="inkling")
    assert await service.drop_trailing_deferral(conversation) is None


async def test_fork_copies_history_preserving_trailing_deferral() -> None:
    service = ConversationManager()
    source = await service.ensure(await _thread(), agent_tentacle_id="inkling")
    run_id = "run-src"
    await service.record_agent_run(
        source,
        run_id=run_id,
        messages=[
            RawModelRequest(
                parts=[UserPromptPart(content="hi")],
                run_id=run_id,
                timestamp=datetime.now(UTC),
            ),
            RawModelResponse(
                parts=[
                    ToolCallPart(
                        tool_name="teleport",
                        args={"hint": "let's take this to a thread"},
                        tool_call_id="call_tp",
                    )
                ],
                run_id=run_id,
                timestamp=datetime.now(UTC),
                model_name="scripted",
                finish_reason="tool_call",
            ),
        ],
    )
    source = await service.ensure(await _thread(), agent_tentacle_id="inkling")

    target_thread = await a_thread("fork-target")
    target = await service.ensure(target_thread, agent_tentacle_id="inkling")
    run = await service.fork(source, target)
    assert run is not None

    # The target holds the forked history, including the trailing teleport deferral,
    # so a resume against it is valid.
    cold = await ConversationManager().ensure(
        target_thread, agent_tentacle_id="inkling"
    )
    assert [type(m).__name__ for m in cold.messages] == [
        "ModelRequest",
        "ModelResponse",
    ]
    # Non-trivial columns copy verbatim, not just id/parts.
    assert next(iter(cold.messages)).message_text == "hi"
    last = list(cold.messages)[-1]
    assert any(
        isinstance(part, ToolCallPart) and part.tool_name == "teleport"
        for part in last.parts
    )
    assert last.model_name == "scripted"
    assert last.finish_reason == "tool_call"

    # The origin is untouched — a fork, not a move.
    origin = await ConversationManager().ensure(
        await _thread(), agent_tentacle_id="inkling"
    )
    assert len(list(origin.messages)) == 2


async def test_fork_rejects_non_empty_target() -> None:
    service = ConversationManager()
    source = await service.ensure(await _thread(), agent_tentacle_id="inkling")
    await service.record_agent_run(
        source,
        run_id="run-src",
        messages=[
            RawModelResponse(
                parts=[TextPart(content="hello")],
                run_id="run-src",
                timestamp=datetime.now(UTC),
                finish_reason="stop",
            ),
        ],
    )
    source = await service.ensure(await _thread(), agent_tentacle_id="inkling")

    target_thread = await a_thread("fork-target")
    target = await service.ensure(target_thread, agent_tentacle_id="inkling")
    await service.record_agent_run(
        target,
        run_id="run-existing",
        messages=[
            RawModelResponse(
                parts=[TextPart(content="prior")],
                run_id="run-existing",
                timestamp=datetime.now(UTC),
                finish_reason="stop",
            ),
        ],
    )
    target = await service.ensure(target_thread, agent_tentacle_id="inkling")

    with pytest.raises(ValueError, match="splice histories"):
        await service.fork(source, target)


async def test_fork_empty_source_is_noop() -> None:
    service = ConversationManager()
    source = await service.ensure(await _thread(), agent_tentacle_id="inkling")
    target = await service.ensure(
        await a_thread("fork-target"), agent_tentacle_id="inkling"
    )
    assert await service.fork(source, target) is None


async def test_a_subagent_conversation_is_its_own_context() -> None:
    """A subagent's conversation is distinct from the agent's own — in the
    database and in the cache — so a child's history can never flatten into
    the parent agent's model context."""
    service = ConversationManager()
    own = await service.ensure(await _thread(), agent_tentacle_id="claude")
    child = await service.ensure(
        await _thread(),
        agent_tentacle_id="claude",
        subagent_id="agent-abc123",
        parent_conversation_id=own.id,
    )
    assert child.id != own.id
    assert child.subagent_id == "agent-abc123"
    assert child.parent_conversation_id == own.id
    assert own.subagent_id == ""
    assert own.parent_conversation_id is None

    # Re-ensuring each identity returns its own conversation, not the other's.
    assert (
        await service.ensure(await _thread(), agent_tentacle_id="claude")
    ).id == own.id
    again = await service.ensure(
        await _thread(),
        agent_tentacle_id="claude",
        subagent_id="agent-abc123",
        parent_conversation_id=own.id,
    )
    assert again.id == child.id

    # And a fresh manager resolves both from the database the same way.
    fresh = ConversationManager()
    assert (
        await fresh.ensure(await _thread(), agent_tentacle_id="claude")
    ).id == own.id
    assert (
        await fresh.ensure(
            await _thread(),
            agent_tentacle_id="claude",
            subagent_id="agent-abc123",
            parent_conversation_id=own.id,
        )
    ).id == child.id


async def test_a_subagent_names_its_parent_or_neither() -> None:
    """The is-subagent fact is encoded once: a child names the conversation
    that spawned it, a bare conversation names nothing. Half-set states are
    refused rather than stored."""
    service = ConversationManager()
    own = await service.ensure(await _thread(), agent_tentacle_id="inkling")
    with pytest.raises(ValueError, match="requires both subagent_id and"):
        await service.ensure(
            await _thread(), agent_tentacle_id="codex", subagent_id="repo-audit"
        )
    with pytest.raises(ValueError, match="a bare conversation takes neither"):
        await service.ensure(
            await _thread(), agent_tentacle_id="codex", parent_conversation_id=own.id
        )


async def test_one_bare_conversation_per_agent_is_enforced() -> None:
    """The empty subagent_id is a value, not a NULL, exactly so the plain
    unique constraint can catch a duplicate bare (thread, agent) row — NULLs
    are distinct in a unique constraint and would wave it through. If this
    raises nothing, the invariant the conversation cache rests on is gone."""
    await ConversationManager().ensure(await _thread(), agent_tentacle_id="inkling")
    async with async_session() as session:
        session.add(
            Conversation(thread_id=await _thread(), agent_tentacle_id="inkling")
        )
        with pytest.raises(sqlalchemy.exc.IntegrityError):
            await session.commit()


async def test_one_conversation_per_subagent_is_enforced() -> None:
    service = ConversationManager()
    parent = await service.ensure(await _thread(), agent_tentacle_id="inkling")
    await service.ensure(
        await _thread(),
        agent_tentacle_id="codex",
        subagent_id="repo-audit",
        parent_conversation_id=parent.id,
    )
    async with async_session() as session:
        session.add(
            Conversation(
                thread_id=await _thread(),
                agent_tentacle_id="codex",
                subagent_id="repo-audit",
            )
        )
        with pytest.raises(sqlalchemy.exc.IntegrityError):
            await session.commit()


async def test_a_run_remembers_which_call_spawned_it() -> None:
    """The run tree: a child run names its parent run and the tool call it
    answers, and both survive a round trip through a fresh manager."""
    service = ConversationManager()
    parent = await service.ensure(await _thread(), agent_tentacle_id="inkling")
    parent_run = await service.record_agent_run(
        parent,
        str(uuid7()),
        [
            RawModelRequest(parts=[UserPromptPart(content="audit the repo")]),
            RawModelResponse(parts=[TextPart(content="commissioning codex")]),
        ],
    )
    assert parent_run is not None

    child = await service.ensure(
        await _thread(),
        agent_tentacle_id="codex",
        subagent_id="repo-audit",
        parent_conversation_id=parent.id,
    )
    recorded = await service.record_agent_run(
        child,
        str(uuid7()),
        [
            RawModelRequest(parts=[UserPromptPart(content="audit these files")]),
            RawModelResponse(parts=[TextPart(content="two findings")]),
        ],
        parent_run_id=parent_run.id,
        parent_tool_call_id="call-1",
    )
    assert recorded is not None

    fresh = await ConversationManager().ensure(
        await _thread(),
        agent_tentacle_id="codex",
        subagent_id="repo-audit",
        parent_conversation_id=parent.id,
    )
    [reloaded] = fresh.runs
    assert reloaded.parent_run_id == parent_run.id
    assert reloaded.parent_tool_call_id == "call-1"
    # The parent agent's own conversation never sees the child's messages.
    parent_fresh = await ConversationManager().ensure(
        await _thread(), agent_tentacle_id="inkling"
    )
    assert [m.message_text for m in parent_fresh.messages] == [
        "audit the repo",
        "commissioning codex",
    ]


async def test_a_posture_written_on_a_conversation_survives_every_later_ensure() -> (
    None
):
    """The conversation is the executor and remembers what its permission is now, so
    nothing re-derives it: a cold manager reads the row back rather than resetting it."""
    thread = await _thread()
    seeded = await ConversationManager().ensure(thread, agent_tentacle_id="claude")
    assert seeded.permission_mode is None

    async with async_session() as session:
        stored = await session.get(Conversation, seeded.id)
        assert stored is not None
        stored.permission_mode = "bypassPermissions"
        await session.commit()

    reloaded = await ConversationManager().ensure(thread, agent_tentacle_id="claude")
    assert reloaded.permission_mode == "bypassPermissions"


async def test_setting_a_posture_persists_it_and_clearing_it_hands_the_default_back() -> (
    None
):
    service = ConversationManager()
    thread = await _thread()
    convo = await service.ensure(thread, agent_tentacle_id="claude")

    await service.set_permission_mode(convo, "bypassPermissions")
    assert convo.permission_mode == "bypassPermissions"
    reloaded = await ConversationManager().ensure(thread, agent_tentacle_id="claude")
    assert reloaded.permission_mode == "bypassPermissions"

    # None is a real answer: the conversation declares nothing again, and the
    # agent's configured default decides as it did before anyone switched.
    await service.set_permission_mode(convo, None)
    cleared = await ConversationManager().ensure(thread, agent_tentacle_id="claude")
    assert cleared.permission_mode is None


async def test_setting_another_providers_posture_is_refused_before_the_write() -> None:
    service = ConversationManager()
    thread = await _thread()
    convo = await service.ensure(thread, agent_tentacle_id="claude")

    with pytest.raises(ValueError, match="not one of claude's modes"):
        await service.set_permission_mode(convo, "auto_review")

    assert convo.permission_mode is None
    reloaded = await ConversationManager().ensure(thread, agent_tentacle_id="claude")
    assert reloaded.permission_mode is None


@pytest.mark.parametrize(
    ("agent_tentacle_id", "permission_mode", "message"),
    [
        # Each provider keeps its own vocabulary, and the row's frozen agent is what
        # says which one it is entitled to.
        ("claude", "auto_review", "not one of claude's modes"),
        ("codex", "bypassPermissions", "not one of codex's modes"),
        ("inkling", "plan", "not one of inkling's modes"),
        ("claude-native", "default", "has no permission modes"),
    ],
)
def test_a_posture_must_be_its_own_agents(
    agent_tentacle_id: str, permission_mode: str, message: str
) -> None:
    with pytest.raises(ValidationError, match=message):
        Conversation(
            thread_id=uuid7(),
            agent_tentacle_id=agent_tentacle_id,
            permission_mode=permission_mode,  # pyright: ignore[reportArgumentType]
        )
