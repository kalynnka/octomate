"""Unit tests for the human-in-the-loop deferred suspender.

`ReflexSuspender` is the policy react invokes (via `ResolveDeferred`) when an
agent run yields `DeferredToolRequests` and no in-process resolver is configured:
it persists a batch + presents it through the channel, then records the batch id
so the caller can report the suspended run.
"""

from __future__ import annotations

from typing import cast

import pytest
from pydantic_ai.messages import ToolCallPart
from pydantic_ai.tools import DeferredToolRequests
from uuid_utils.compat import uuid7

from octomate.capabilities.harness.events import ActionBatchEvent
from octomate.managers.deferred import DeferredActionManager
from octomate.reflex.suspender import ReflexSuspender
from octomate.schemas.conversation import ChannelAddress
from octomate.schemas.deferred import (
    ApprovalRequest,
    DeferredActionBatch,
    DeferredApproval,
    DeferredQuestion,
)
from octomate.schemas.triage import (
    TELEPORT_DEFER_KIND,
    SummonDecision,
    TeleportDecision,
)
from tests.support.channels import FakeChannelTentacle
from tests.support.managers import (
    FakeActionManager,
    FakeConversationManager,
    FakePresentedBatch,
)


def _key() -> ChannelAddress:
    return ChannelAddress(
        channel_tentacle_id="im",
        chat_type="dm",
        chat_id="alice",
        user_id="alice",
    )


def _requests() -> DeferredToolRequests:
    return DeferredToolRequests(
        calls=[
            ToolCallPart(
                tool_name="ask_questions",
                args={"questions": [{"question": "What should I clarify?"}]},
                tool_call_id="call_question",
            )
        ]
    )


async def test_human_review_suspender_persists_batch_and_records_id() -> None:
    address = _key()
    thread_id = uuid7()
    requests = _requests()
    conversations = FakeConversationManager()
    action_manager = FakeActionManager()
    channel = FakeChannelTentacle()
    decision = SummonDecision(
        action="summon",
        agent_id="inkling",
        model="test",
        reason="needs input",
        hint="needs input",
        summon="needs input",
    )

    suspender = ReflexSuspender(
        channel=channel,
        action_manager=cast(DeferredActionManager, action_manager),
        conversation_manager=conversations,
        agent_tentacle_id="inkling",
        run_name="react",
        source_address=address,
        target_address=address,
        target_mode="sub",
        decision=decision,
        thread_id=thread_id,
    )

    assert suspender.suspended_batch_id is None
    await suspender.suspend(requests)

    assert len(action_manager.create_calls) == 1
    call = action_manager.create_calls[0]
    assert call.run_name == "react"
    assert call.source_address == address
    assert call.target_address == address
    assert call.target_mode == "sub"
    assert call.decision is decision
    assert call.requests is requests
    assert suspender.suspended_batch_id == call.batch_id
    assert conversations.ensured == [(thread_id, "inkling")]


async def test_suspender_emit_on_stream_returns_batch_event_without_rendering() -> None:
    address = _key()
    question = DeferredQuestion(
        tool_name="ask_questions",
        tool_call_id="c1",
        args={"question": "What should I clarify?"},
    )
    approval = DeferredApproval(
        tool_name="do_thing",
        tool_call_id="c2",
        args=ApprovalRequest(tool_name="do_thing"),
    )
    batch = FakePresentedBatch(questions=[question], approvals=[approval])
    channel = FakeChannelTentacle()

    suspender = ReflexSuspender(
        channel=channel,
        action_manager=cast(
            DeferredActionManager, FakeActionManager(presented_batch=batch)
        ),
        conversation_manager=FakeConversationManager(),
        agent_tentacle_id="inkling",
        run_name="react",
        source_address=address,
        target_address=address,
        target_mode="sub",
        decision=None,
        thread_id=uuid7(),
        emit_on_stream=True,
    )

    event = await suspender.suspend(_requests())

    # Persisted (batch id recorded) and handed back as one event — rendered nothing
    # through the channel (that is the consumer's job).
    assert suspender.suspended_batch_id == batch.id
    assert channel.sent == []

    assert isinstance(event, ActionBatchEvent)
    assert event.batch_id == str(batch.id)
    assert event.questions == [question]
    assert event.approvals == [approval]


@pytest.mark.parametrize("surface", ["dm", "group", "thread"])
async def test_a_teleport_goes_to_the_graph_with_its_validated_destination(
    surface: str,
) -> None:
    destination = ChannelAddress(
        channel_tentacle_id="far",
        chat_type="dm"
        if surface == "dm"
        else "group"
        if surface == "group"
        else "thread",
        chat_id="" if surface == "dm" else "parent",
        user_id="alice",
        shared=surface == "group",
    )
    decision = TeleportDecision(
        agent_id="inkling", hint="Continue", destination=destination
    )
    action_manager = FakeActionManager()
    suspender = ReflexSuspender(
        channel=FakeChannelTentacle(),
        action_manager=cast(DeferredActionManager, action_manager),
        conversation_manager=FakeConversationManager(),
        agent_tentacle_id="inkling",
        run_name="react",
        source_address=_key(),
        target_address=_key(),
        target_mode="main",
        decision=None,
        thread_id=uuid7(),
    )

    assert await suspender.suspend(decision.deferral("move")) is None

    assert suspender.teleport is not None
    assert suspender.teleport.destination == destination
    assert suspender.teleport.tool_call_id == "move"
    assert suspender.suspended_batch_id is None
    assert action_manager.create_calls == []


async def test_a_teleport_deferral_without_a_destination_is_refused() -> None:
    """The gateway resolves an omitted destination to the current conversation before
    the decision exists, so a deferral naming none is a wiring bug, not "here"."""
    nowhere = DeferredToolRequests(
        calls=[ToolCallPart(tool_name="teleport", args={}, tool_call_id="move")],
        metadata={"move": {"kind": TELEPORT_DEFER_KIND, "hint": "Continue"}},
    )
    suspender = ReflexSuspender(
        channel=FakeChannelTentacle(),
        action_manager=cast(DeferredActionManager, FakeActionManager()),
        conversation_manager=FakeConversationManager(),
        agent_tentacle_id="inkling",
        run_name="react",
        source_address=_key(),
        target_address=_key(),
        target_mode="main",
        decision=None,
        thread_id=uuid7(),
    )

    with pytest.raises(ValueError, match="names no destination"):
        await suspender.suspend(nowhere)


async def test_a_teleport_beside_another_deferral_is_refused() -> None:
    deferral = TeleportDecision(
        agent_id="inkling", hint="Continue", destination=_key()
    ).deferral("move")
    suspender = ReflexSuspender(
        channel=FakeChannelTentacle(),
        action_manager=cast(DeferredActionManager, FakeActionManager()),
        conversation_manager=FakeConversationManager(),
        agent_tentacle_id="inkling",
        run_name="react",
        source_address=_key(),
        target_address=_key(),
        target_mode="main",
        decision=None,
        thread_id=uuid7(),
    )

    with pytest.raises(RuntimeError, match="beside other calls"):
        await suspender.suspend(
            DeferredToolRequests(
                calls=[*_requests().calls, *deferral.calls],
                metadata=deferral.metadata,
            )
        )


@pytest.mark.parametrize("streamed", [True, False])
async def test_a_live_run_pauses_on_its_batch_without_suspending(
    streamed: bool,
) -> None:
    address = _key()
    question = DeferredQuestion(
        tool_name="ask_questions",
        tool_call_id="c1",
        args={"question": "What should I clarify?"},
    )
    batch = FakePresentedBatch(questions=[question])
    channel = FakeChannelTentacle()
    suspender = ReflexSuspender(
        channel=channel,
        action_manager=cast(
            DeferredActionManager, FakeActionManager(presented_batch=batch)
        ),
        conversation_manager=FakeConversationManager(),
        agent_tentacle_id="claude",
        run_name="react",
        source_address=address,
        target_address=address,
        target_mode="main",
        decision=None,
        thread_id=uuid7(),
        emit_on_stream=streamed,
    )

    batch_id = uuid7()
    paused, event = await suspender.pause(_requests(), batch_id=batch_id)

    assert paused is batch
    # The batch is the one the caller already waits on, and its reply is live.
    assert (paused.id, paused.response_mode) == (batch_id, "live")
    # The run stays live, so nothing records it as ended suspended.
    assert suspender.suspended_batch_id is None
    if streamed:
        # The run's own stream presents it; the channel is not touched.
        assert event == ActionBatchEvent.from_batch(cast(DeferredActionBatch, batch))
        assert channel.sent == []
    else:
        # Nothing draws the run, so the channel shows the cards now.
        assert event is None
        assert "What should I clarify?" in channel.sent[0][2][0]["text"]
