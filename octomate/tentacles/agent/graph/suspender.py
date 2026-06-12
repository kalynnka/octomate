from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Literal

import logfire
from pydantic_ai.tools import DeferredToolRequests

from octomate.capabilities.events import ActionBatchEvent
from octomate.managers.conversations import ConversationManager
from octomate.managers.deferred import DeferredActionManager
from octomate.schemas.conversation import ConversationKey
from octomate.schemas.triage import ResponseTargetMode, TriageDecision
from octomate.tentacles.channel.base import ChannelTentacle


@dataclass
class HumanReviewSuspender:
    """`DeferredSuspender` that persists a batch and presents it via the
    channel for human review, then leaves the run suspended. Triage builds it
    with the right run context; react invokes it (via `ResolveDeferred`) when an
    agent run yields `DeferredToolRequests` and no in-process resolver is set.
    """

    channel: ChannelTentacle
    action_manager: DeferredActionManager
    conversation_manager: ConversationManager
    agent_tentacle_id: str
    run_name: Literal["triage", "reception"]
    source_key: ConversationKey
    target_key: ConversationKey
    target_mode: ResponseTargetMode
    decision: TriageDecision | None
    emit_on_stream: bool = False
    suspended_batch_id: uuid.UUID | None = field(default=None, init=False)

    async def suspend(self, requests: DeferredToolRequests) -> ActionBatchEvent | None:
        with logfire.span(
            "suspend_for_review",
            run_name=self.run_name,
            agent_id=self.agent_tentacle_id,
            target_key=str(self.target_key),
            source_key=str(self.source_key),
            emit_on_stream=self.emit_on_stream,
        ) as span:
            conversation = await self.conversation_manager.ensure(
                self.target_key,
                agent_tentacle_id=self.agent_tentacle_id,
            )
            if self.emit_on_stream:
                # On-stream round-trip: persist the batch and hand it back as one
                # event for the consumer to render + mark as a unit.
                batch = await self.action_manager.create_batch(
                    conversation=conversation,
                    agent_tentacle_id=self.agent_tentacle_id,
                    run_name=self.run_name,
                    source_key=self.source_key,
                    target_key=self.target_key,
                    target_mode=self.target_mode,
                    decision=self.decision,
                    requests=requests,
                )
                self.suspended_batch_id = batch.id
                span.set_attribute("batch_id", str(batch.id))
                return ActionBatchEvent(
                    batch_id=str(batch.id),
                    questions=list(batch.questions),
                    approvals=list(batch.approvals),
                )

            batch = await self.channel.feelers.present_actions(
                action_manager=self.action_manager,
                conversation=conversation,
                agent_tentacle_id=self.agent_tentacle_id,
                run_name=self.run_name,
                source_key=self.source_key,
                target_key=self.target_key,
                target_mode=self.target_mode,
                decision=self.decision,
                requests=requests,
            )
            self.suspended_batch_id = batch.id
            span.set_attribute("batch_id", str(batch.id))
