"""The reflex graph's deferred suspender: where a run's deferrals go — a teleport
back to the graph, everything else to a human as a persisted batch."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field

from pydantic_ai.tools import DeferredToolRequests

from octomate.capabilities.harness.events import ActionBatchEvent
from octomate.managers.conversation import ConversationManager
from octomate.managers.deferred import DeferredActionManager
from octomate.schemas.conversation import ChannelAddress
from octomate.schemas.triage import (
    TELEPORT_DEFER_KIND,
    ResponseTargetMode,
    RunName,
    SummonDecision,
)
from octomate.telemetry import reflex_logfire
from octomate.tentacles.channel import ChannelTentacle


@dataclass(frozen=True)
class TeleportRequest:
    """A teleport for the graph to perform: carry the history and resume the agent
    in the new place. One shape for every runtime — an Inkling run defers the
    `teleport` call itself; a runtime a tool result cannot suspend is interrupted
    on the recorded decision and ends its turn as the same deferral — read off
    the deferred call by its metadata kind."""

    hint: str
    # The deferred call to resolve into the resumed run.
    tool_call_id: str | None
    # None uses the run's current address as the parent.
    destination: ChannelAddress | None = None
    new_thread: bool = True
    # The project the thread landed in is bound to, and the ref its workspace starts
    # from; None carries the conversation only.
    project: str | None = None
    ref: str | None = None
    # Whether the agent carries on at once where it lands.
    resume: bool = False

    @classmethod
    def of(cls, requests: DeferredToolRequests) -> TeleportRequest | None:
        """The teleport these requests carry."""
        for call in requests.calls:
            meta = requests.metadata.get(call.tool_call_id, {})
            if meta.get("kind") != TELEPORT_DEFER_KIND:
                continue
            destination = meta.get("destination")
            return cls(
                tool_call_id=call.tool_call_id,
                hint=str(meta.get("hint") or ""),
                new_thread=bool(meta.get("new_thread", True)),
                project=str(meta.get("project") or "") or None,
                ref=str(meta.get("ref") or "") or None,
                resume=bool(meta.get("resume", False)),
                # The address validates itself, being a pydantic dataclass.
                destination=ChannelAddress(**destination) if destination else None,
            )
        return None


@dataclass
class ReflexSuspender:
    """The reflex graph's `DeferredSuspender`: every deferral a run ends on comes
    through here once, and each kind goes where it is resolved — a `teleport` to
    the graph, which performs it and resumes the agent; anything else to a human,
    persisted as a batch and presented on the channel. React builds it with the
    run's context; Inkling reaches it through `ResolveDeferred`, a runtime a tool
    result cannot suspend through the `deferred_suspender` its run was handed.
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
    # The deferred `teleport`, for the graph to perform instead of a batch.
    teleport: TeleportRequest | None = field(default=None, init=False)

    async def suspend(self, requests: DeferredToolRequests) -> ActionBatchEvent | None:
        teleport = TeleportRequest.of(requests)
        if teleport is not None:
            # The agent's own permission check already let the call through, so
            # nothing here asks: the graph moves it once the run ends.
            if len(requests.calls) + len(requests.approvals) > 1:
                raise RuntimeError("a teleport was deferred beside other calls")
            self.teleport = teleport
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
