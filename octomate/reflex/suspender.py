from __future__ import annotations

import uuid
from dataclasses import dataclass, field

from pydantic_ai.tools import DeferredToolRequests

from octomate.capabilities.events import ActionBatchEvent
from octomate.capabilities.gate import TELEPORT_KIND
from octomate.managers.conversation import ConversationManager
from octomate.managers.deferred import DeferredActionManager
from octomate.schemas.conversation import ChannelAddress
from octomate.schemas.triage import ResponseTargetMode, RunName, SummonDecision
from octomate.telemetry import reflex_logfire
from octomate.tentacles.channel.base import ChannelTentacle


@dataclass(frozen=True)
class TeleportRequest:
    """A `teleport` deferral, classified out of a run's `DeferredToolRequests` by its
    declared metadata kind — the graph resolves this call and relocates the run."""

    tool_call_id: str
    hint: str


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
    run_name: RunName
    source_address: ChannelAddress
    target_address: ChannelAddress
    target_mode: ResponseTargetMode
    decision: SummonDecision | None
    thread_id: uuid.UUID | None = None
    emit_on_stream: bool = False
    suspended_batch_id: uuid.UUID | None = field(default=None, init=False)
    # Set when a run deferred a `teleport` (classified by metadata kind); the dispatch
    # graph reads this to route to its Teleport node instead of persisting a batch.
    teleport: TeleportRequest | None = field(default=None, init=False)

    async def suspend(self, requests: DeferredToolRequests) -> ActionBatchEvent | None:
        # `teleport` declares kind="teleport" in its CallDeferred metadata — the graph
        # resolves it (fork + resume), not a human. Classify by the declared kind (not
        # a tool name), stash it typed, and let run1 end so it bubbles to the dispatch.
        for call in requests.calls:
            meta = requests.metadata.get(call.tool_call_id, {})
            if meta.get("kind") == TELEPORT_KIND:
                self.teleport = TeleportRequest(
                    tool_call_id=call.tool_call_id,
                    hint=str(meta.get("hint") or ""),
                )
                return None
        with reflex_logfire.span(
            "suspend_for_review",
            run_name=self.run_name,
            agent_id=self.agent_tentacle_id,
            target_address=str(self.target_address),
            source_address=str(self.source_address),
            emit_on_stream=self.emit_on_stream,
        ) as span:
            if self.thread_id is None:
                raise ValueError("deferred review requires a thread_id")
            conversation = await self.conversation_manager.ensure(
                self.thread_id,
                agent_tentacle_id=self.agent_tentacle_id,
            )
            if self.emit_on_stream:
                # On-stream round-trip: persist the batch and hand it back as one
                # event for the consumer to render + mark as a unit.
                batch = await self.action_manager.create_batch(
                    conversation=conversation,
                    agent_tentacle_id=self.agent_tentacle_id,
                    run_name=self.run_name,
                    source_address=self.source_address,
                    target_address=self.target_address,
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
                source_address=self.source_address,
                target_address=self.target_address,
                target_mode=self.target_mode,
                decision=self.decision,
                requests=requests,
            )
            self.suspended_batch_id = batch.id
            span.set_attribute("batch_id", str(batch.id))
