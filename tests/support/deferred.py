"""Scripted card responses shared by offline checks and live channel replays."""

from __future__ import annotations

from contextlib import AsyncExitStack
from typing import Literal

from pydantic_ai.messages import ToolCallPart
from pydantic_ai.tools import DeferredToolRequests

from octomate.capabilities.harness.events import ActionBatchEvent
from octomate.schemas.awakes import DeferredActionBatchResponse
from octomate.schemas.conversation import ChannelAddress
from octomate.schemas.deferred import DeferredActionBatch
from octomate.schemas.thread import ThreadKey
from octomate.tentacles.channel import ChannelTentacle
from tests.support.nerve import drained, drawing

type CardUpdateScenario = Literal["answered", "corrected", "expired"]


async def replay_card_updates[RawT, MessageT](
    channel: ChannelTentacle[RawT, MessageT],
    address: ChannelAddress,
    scenario: CardUpdateScenario,
) -> DeferredActionBatch:
    """Post persisted cards, then change them through scripted manager responses.

    The caller supplies an isolated test database. Only the channel transport is
    live in trigger tests; no model or platform button callback is invoked.
    Each update finishes rendering before the next change is persisted. Replays
    use the enclosing test's timeout so real platform calls can finish.
    """
    manager = channel.octomate.deferred_actions
    thread = await channel.octomate.threads.ensure(ThreadKey.from_address(address))
    conversation = await channel.octomate.conversations.ensure(
        thread.id, agent_tentacle_id="trigger"
    )
    batch = await manager.create_batch(
        conversation=conversation,
        agent_tentacle_id="trigger",
        run_name="trigger",
        source_address=address,
        target_address=address,
        target_mode="main",
        decision=None,
        response_mode="live",
        requests=DeferredToolRequests(
            approvals=[
                ToolCallPart("shell", {"cmd": "pwd"}, "script-approve"),
                ToolCallPart("shell", {"cmd": "git status"}, "script-deny"),
            ],
            calls=[
                ToolCallPart(
                    "ask_questions",
                    {
                        "questions": [
                            {
                                "question": f"Scripted {scenario}: which regions?",
                                "choices": ["us", "eu"],
                                "multi_select": True,
                            },
                            {
                                "question": "Submit this scripted form?",
                                "choices": ["Save draft", "Submit", "Cancel"],
                            },
                        ]
                    },
                    "script-questions",
                )
            ],
        ),
    )
    await channel.feelers.present_actions(
        address, ActionBatchEvent.from_batch(batch), action_manager=manager
    )
    batch = await manager.get_batch(batch.id)
    assert all(
        action.platform_message_id for action in [*batch.questions, *batch.approvals]
    ), "a scripted card was not posted"

    async with AsyncExitStack() as stack:
        if channel.siphon.reading is None or channel.siphon.reading.done():
            await stack.enter_async_context(drawing(channel))
        if scenario == "expired":
            await manager.mark_batch(batch.id, "expired")
            await drained(channel)
            return await manager.get_batch(batch.id)

        region, control = sorted(batch.questions)
        approved, denied = batch.approvals
        await manager.resolve_batch(
            DeferredActionBatchResponse(
                batch_id=batch.id,
                answers={region.id: ["eu"], control.id: "Submit"},
                approvals={approved.id: True, denied.id: False},
            ),
            finalize=scenario == "answered",
        )
        if scenario == "corrected":
            await manager.review_questions(
                batch.id,
                errors={region.id: "Scripted validation: choose us instead."},
                reset=control.id,
            )
            await drained(channel)
            await manager.resolve_batch(
                DeferredActionBatchResponse(
                    batch_id=batch.id,
                    answers={region.id: ["us"], control.id: "Submit"},
                ),
                finalize=False,
            )
            await manager.submit_questions(batch.id)
        await drained(channel)
    return await manager.get_batch(batch.id)
